from __future__ import annotations

import functools
import hashlib
import json
import math
import re
import warnings
import socket
import time
import urllib.error
import urllib.request
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timezone
from threading import BoundedSemaphore, Lock
from typing import Any, Callable, Dict, Iterator, List, Sequence

from .models import Chunk
from .storage import SqliteStore
from .vector_store import SqliteVectorStore, VectorStore, dedupe_vector_stores

# 流式 embed 时 SQLite commit 间隔（成功 batch 数）。过小会严重拖慢；可在 embedding.stream_commit_every_batches 覆盖。
DEFAULT_STREAM_COMMIT_EVERY_BATCHES = 2000

_CHUNK_TOKEN_WORD_RE = re.compile(r"\w+|[^\w\s]")


def heuristic_chunk_token_count(text: str) -> int:
    """按行 regex 近似：与旧版 _line_token_count 按行累加一致（每行至少 1）。"""
    if not text:
        return 0
    return sum(max(1, len(_CHUNK_TOKEN_WORD_RE.findall(line))) for line in text.split("\n"))


@functools.lru_cache(maxsize=16)
def _voyage_hf_tokenizer_for_model(model: str):
    from tokenizers import Tokenizer  # type: ignore[import-untyped]

    tok = Tokenizer.from_pretrained(f"voyageai/{model}")
    tok.no_truncation()
    return tok


def _voyage_chunk_token_counter(model: str) -> Callable[[str], int]:
    tok = _voyage_hf_tokenizer_for_model(model)

    def count(text: str) -> int:
        if not text:
            return 0
        return len(tok.encode(text).ids)

    return count


def make_chunk_token_count_fn(*, backend: str, model: str | None) -> Callable[[str], int]:
    """切块用 token 计数：voyage 使用 HF `voyageai/{model}`；否则回退启发式。"""
    b = (backend or "auto").strip().lower()
    m = (model or "").strip()
    resolved = "voyage" if b == "auto" and m.startswith("voyage-") else b
    if resolved == "auto":
        resolved = "heuristic"
    if resolved == "voyage":
        if not m:
            warnings.warn(
                "chunk.token_counter=voyage/auto 但未提供有效的 embedding.model 或 chunk.token_counter_model，"
                "已回退 heuristic_chunk_token_count",
                stacklevel=2,
            )
            return heuristic_chunk_token_count
        try:
            return _voyage_chunk_token_counter(m)
        except Exception as exc:  # pragma: no cover
            warnings.warn(
                f"无法加载 voyageai/{m} tokenizer（{exc!r}），已回退 heuristic_chunk_token_count",
                stacklevel=2,
            )
            return heuristic_chunk_token_count
    return heuristic_chunk_token_count


def _joined_lines_token_count(lines: Sequence[str], token_count: Callable[[str], int]) -> int:
    if not lines:
        return 0
    return int(token_count("\n".join(lines)))

JAVA_AST_DECLARATION_TYPES = {
    "class_declaration",
    "interface_declaration",
    "enum_declaration",
    "record_declaration",
    "annotation_type_declaration",
    "constructor_declaration",
    "method_declaration",
    "field_declaration",
}

CODE_FILE_SUFFIXES = {
    ".c",
    ".cc",
    ".cpp",
    ".cs",
    ".cxx",
    ".go",
    ".h",
    ".hh",
    ".hpp",
    ".java",
    ".js",
    ".jsx",
    ".kt",
    ".kts",
    ".m",
    ".mm",
    ".php",
    ".py",
    ".rb",
    ".rs",
    ".scala",
    ".swift",
    ".ts",
    ".tsx",
}

CODE_LANGUAGES = {
    "c",
    "cpp",
    "csharp",
    "go",
    "java",
    "javascript",
    "js",
    "kotlin",
    "objective-c",
    "php",
    "python",
    "ruby",
    "rust",
    "scala",
    "swift",
    "ts",
    "tsx",
    "typescript",
}


class EmbeddingProviderError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        category: str = "unknown",
        status_code: int | None = None,
        retry_after_s: float | None = None,
    ) -> None:
        super().__init__(message)
        self.category = category
        self.status_code = status_code
        self.retry_after_s = retry_after_s


def resolve_llama_init_kwargs(
    init_kwargs: Dict[str, object] | None,
    common_arg_map: Dict[str, object] | None,
    common_values: Dict[str, object],
) -> Dict[str, object]:
    merged = dict(init_kwargs or {})
    arg_map = common_arg_map or {}
    for source_key, target_name in arg_map.items():
        if not isinstance(target_name, str):
            continue
        target = target_name.strip()
        if not target:
            continue
        if source_key not in common_values:
            continue
        value = common_values[source_key]
        if value in ("", None, 0):
            continue
        existing = merged.get(target)
        if existing in ("", None, 0):
            merged[target] = value
    return merged


def _parse_retry_after_seconds(value: str | None) -> float | None:
    if not value:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        return max(0.0, float(text))
    except ValueError:
        pass
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        from email.utils import parsedate_to_datetime

        try:
            dt = parsedate_to_datetime(text)
        except (TypeError, ValueError, IndexError):
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return max(0.0, (dt - datetime.now(timezone.utc)).total_seconds())


def _read_http_error_body(exc: urllib.error.HTTPError) -> str:
    try:
        body = exc.read().decode("utf-8", errors="ignore").strip()
    except Exception:
        return ""
    if len(body) > 300:
        body = body[:300] + "..."
    return body


def _truncate_debug_text(value: object, limit: int = 500) -> str:
    text = str(value).strip()
    if not text:
        return ""
    if len(text) > limit:
        return text[:limit] + "..."
    return text


def _extract_exception_response_detail(exc: Exception) -> str:
    if isinstance(exc, urllib.error.HTTPError):
        return _read_http_error_body(exc)
    for attr in ("body", "detail", "message"):
        value = getattr(exc, attr, None)
        if isinstance(value, str) and value.strip():
            return _truncate_debug_text(value)
    response = getattr(exc, "response", None)
    if response is None:
        return ""
    text = getattr(response, "text", None)
    if isinstance(text, str) and text.strip():
        return _truncate_debug_text(text)
    content = getattr(response, "content", None)
    if isinstance(content, bytes):
        try:
            decoded = content.decode("utf-8", errors="ignore").strip()
        except Exception:
            decoded = ""
        if decoded:
            return _truncate_debug_text(decoded)
    return ""


def _exception_chain(exc: Exception, max_depth: int = 4) -> List[Exception]:
    chain: List[Exception] = []
    seen: set[int] = set()
    current: Exception | None = exc
    while current is not None and len(chain) < max_depth:
        marker = id(current)
        if marker in seen:
            break
        seen.add(marker)
        chain.append(current)
        next_exc = getattr(current, "__cause__", None) or getattr(current, "__context__", None)
        current = next_exc if isinstance(next_exc, Exception) else None
    return chain


def _format_exception_debug(exc: Exception) -> str:
    parts: List[str] = []
    for idx, current in enumerate(_exception_chain(exc)):
        prefix = "cause" if idx > 0 else "type"
        parts.append(f"{prefix}={current.__class__.__module__}.{current.__class__.__name__}")
        parts.append(f"{'cause_repr' if idx > 0 else 'repr'}={_truncate_debug_text(repr(current))}")
        status_code, retry_after_s = _extract_status_code_and_retry_after(current)
        detail = _extract_exception_response_detail(current)
        if status_code is not None:
            parts.append(f"{'cause_status' if idx > 0 else 'status'}={status_code}")
        if retry_after_s is not None:
            parts.append(f"{'cause_retry_after_s' if idx > 0 else 'retry_after_s'}={retry_after_s}")
        if detail:
            parts.append(f"{'cause_detail' if idx > 0 else 'detail'}={detail}")
    return " ".join(parts)


def _extract_status_code_and_retry_after(exc: Exception) -> tuple[int | None, float | None]:
    retry_after_s: float | None = None
    response = getattr(exc, "response", None)
    headers = getattr(exc, "headers", None)
    if headers is None and response is not None:
        headers = getattr(response, "headers", None)
    if headers is not None:
        header_get = getattr(headers, "get", None)
        if callable(header_get):
            retry_after_s = _parse_retry_after_seconds(header_get("Retry-After"))
    for attr in ("status_code", "status", "http_status", "code"):
        value = getattr(exc, attr, None)
        if isinstance(value, int):
            return value, retry_after_s
        if isinstance(value, str) and value.isdigit():
            return int(value), retry_after_s
    if response is not None:
        value = getattr(response, "status_code", None)
        if isinstance(value, int):
            return value, retry_after_s
        if isinstance(value, str) and value.isdigit():
            return int(value), retry_after_s
    return None, retry_after_s


def _build_http_provider_error(
    status_code: int,
    retry_after_s: float | None,
    detail: str = "",
) -> EmbeddingProviderError:
    message = f"HTTP {status_code}"
    if detail:
        message = f"{message}: {detail}"
    if status_code == 429:
        return EmbeddingProviderError(
            message,
            category="rate_limit",
            status_code=status_code,
            retry_after_s=retry_after_s,
        )
    if status_code >= 500:
        return EmbeddingProviderError(
            message,
            category="server_error",
            status_code=status_code,
            retry_after_s=retry_after_s,
        )
    return EmbeddingProviderError(
        message,
        category="http_error",
        status_code=status_code,
        retry_after_s=retry_after_s,
    )


def _classify_embedding_exception(exc: Exception) -> EmbeddingProviderError:
    if isinstance(exc, EmbeddingProviderError):
        return exc
    if isinstance(exc, urllib.error.HTTPError):
        detail = _read_http_error_body(exc)
        status_code, retry_after_s = _extract_status_code_and_retry_after(exc)
        if status_code is not None:
            return _build_http_provider_error(status_code, retry_after_s, detail)
    if isinstance(exc, urllib.error.URLError):
        reason = exc.reason
        if isinstance(reason, TimeoutError | socket.timeout):
            return EmbeddingProviderError(str(reason), category="timeout")
        return EmbeddingProviderError(str(exc), category="network")
    if isinstance(exc, TimeoutError | socket.timeout):
        return EmbeddingProviderError(str(exc) or "request timed out", category="timeout")
    status_code, retry_after_s = _extract_status_code_and_retry_after(exc)
    if status_code is not None:
        return _build_http_provider_error(status_code, retry_after_s)
    message = str(exc).strip() or exc.__class__.__name__
    lowered = message.lower()
    if "429" in lowered or "too many requests" in lowered or "rate limit" in lowered:
        return EmbeddingProviderError(message, category="rate_limit")
    if "timeout" in lowered or "timed out" in lowered or "deadline exceeded" in lowered:
        return EmbeddingProviderError(message, category="timeout")
    if any(token in lowered for token in ("connection", "temporarily unavailable", "reset by peer", "broken pipe")):
        return EmbeddingProviderError(message, category="network")
    return EmbeddingProviderError(message, category="unknown")


def _sqlite_table_exists(conn: object, name: str) -> bool:
    cur = conn.execute(  # type: ignore[union-attr]
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1",
        (name,),
    )
    return cur.fetchone() is not None


def _leading_doc_comment_start_line(
    lines: List[str],
    definition_start_line: int,
    max_lookback: int,
) -> int:
    """从 definition 行号向上包含 Javadoc / 块注释 / 常见注解行（Java 等）。"""
    if definition_start_line <= 0:
        return 0
    lo = max(0, definition_start_line - max(0, max_lookback))
    i = definition_start_line - 1
    while i >= lo:
        line = lines[i]
        s = line.strip()
        if s == "":
            i -= 1
            continue
        if s.startswith("@"):
            i -= 1
            continue
        if s.startswith("/**") or s.startswith("/*"):
            i -= 1
            continue
        if s.startswith("*") or s == "*/":
            i -= 1
            continue
        if s.startswith("//"):
            i -= 1
            continue
        if s.startswith('"""') or s.startswith("'''"):
            i -= 1
            continue
        break
    return i + 1


def _get_java_parser() -> object | None:
    try:
        from tree_sitter import Language, Parser  # type: ignore
        import tree_sitter_java  # type: ignore
    except Exception:
        return None
    parser = Parser()
    parser.language = Language(tree_sitter_java.language())
    return parser


def _walk_tree_sitter(node: object) -> Iterator[object]:
    stack: list[object] = [node]
    while stack:
        n = stack.pop()
        yield n
        ch = getattr(n, "children", []) or []
        for child in reversed(ch):
            stack.append(child)


def _normalize_java_kind(kind: str) -> set[str]:
    value = (kind or "").lower()
    if "constructor" in value:
        return {"constructor_declaration"}
    if "method" in value or "function" in value:
        return {"method_declaration"}
    if "interface" in value:
        return {"interface_declaration"}
    if "enum" in value:
        return {"enum_declaration"}
    if "field" in value or "property" in value or "constant" in value:
        return {"field_declaration"}
    if "class" in value or "record" in value or "type" in value or "object" in value:
        return {"class_declaration", "record_declaration", "annotation_type_declaration"}
    return set(JAVA_AST_DECLARATION_TYPES)


def _legacy_should_chunk_symbol_kind(kind: str) -> bool:
    value = (kind or "").lower()
    return any(
        token in value
        for token in (
            "constructor",
            "method",
            "function",
            "class",
            "interface",
            "enum",
            "record",
            "type",
            "field",
            "property",
            "constant",
        )
    )


def _is_function_level_symbol_kind(kind: str) -> bool:
    """最小粒度为函数：仅构造函数/方法/函数体可独立成块；字段、常量、类型容器等不归入此列。"""
    v = (kind or "").lower()
    if "constructor" in v:
        return True
    if "method" in v:
        return True
    if "function" in v and "interface" not in v:
        return True
    return False


def _should_chunk_symbol_kind(kind: str, *, function_level_only: bool) -> bool:
    if function_level_only:
        return _is_function_level_symbol_kind(kind)
    return _legacy_should_chunk_symbol_kind(kind)


def _merge_line_intervals(intervals: Sequence[tuple[int, int]]) -> List[tuple[int, int]]:
    cleaned = [(min(a, b), max(a, b)) for a, b in intervals if a < b]
    if not cleaned:
        return []
    cleaned.sort(key=lambda x: (x[0], x[1]))
    merged: List[tuple[int, int]] = [cleaned[0]]
    for s, e in cleaned[1:]:
        ls, le = merged[-1]
        if s <= le:
            merged[-1] = (ls, max(le, e))
        else:
            merged.append((s, e))
    return merged


def _gaps_in_half_open_range(lo: int, hi: int, merged_covered: Sequence[tuple[int, int]]) -> List[tuple[int, int]]:
    """[lo, hi) 内未被 merged_covered（已合并、互不重叠）覆盖的半开区间。"""
    if lo >= hi:
        return []
    cur = lo
    out: List[tuple[int, int]] = []
    for s, e in merged_covered:
        if e <= lo:
            continue
        if s >= hi:
            break
        s2 = max(lo, s)
        e2 = min(hi, e)
        if cur < s2:
            out.append((cur, s2))
        cur = max(cur, e2)
        if cur >= hi:
            return [(a, b) for a, b in out if a < b]
    if cur < hi:
        out.append((cur, hi))
    return [(a, b) for a, b in out if a < b]


def _pack_sibling_function_candidates(
    lines: List[str],
    candidates: List[Dict[str, object]],
    enclosing_by_sid: Dict[str, str],
    doc_id: str,
    *,
    enabled: bool,
    small_max_tokens: int,
    merge_target_tokens: int,
    max_gap_lines: int,
    token_count: Callable[[str], int],
) -> List[Dict[str, object]]:
    """同一 enclosing 下按源码顺序，将过短的相邻函数候选合并为更大、更易检索的 span（retrieval-aware packing）。"""
    if not enabled or not candidates:
        return candidates
    containers = [c for c in candidates if bool(c["is_container"])]
    leaves = [c for c in candidates if not bool(c["is_container"])]
    if not leaves:
        return candidates
    groups: Dict[str, List[Dict[str, object]]] = {}
    for c in leaves:
        sid = str(c["symbol_id"])
        enc = enclosing_by_sid.get(sid, "")
        key = f"{doc_id}\0{enc}" if enc else f"{doc_id}\0__top__"
        groups.setdefault(key, []).append(c)
    packed_leaves: List[Dict[str, object]] = []
    for _key, items in groups.items():
        items.sort(key=lambda x: (int(x["node_start"]), int(x["node_end"])))
        i = 0
        while i < len(items):
            first = items[i]
            t0 = _joined_lines_token_count(list(first["span_lines"]), token_count)  # type: ignore[arg-type]
            if t0 >= small_max_tokens:
                packed_leaves.append(first)
                i += 1
                continue
            lo_eff = int(first["eff_start"])
            hi_end = int(first["node_end"])
            acc = t0
            sids = [str(first["symbol_id"])]
            sigs = [str(first["signature"])]
            ns_lo = int(first["node_start"])
            j = i + 1
            while j < len(items):
                prev, cur = items[j - 1], items[j]
                if int(cur["node_start"]) - int(prev["node_end"]) > max_gap_lines:
                    break
                ct = _joined_lines_token_count(list(cur["span_lines"]), token_count)  # type: ignore[arg-type]
                if ct >= small_max_tokens:
                    break
                lo_eff = min(lo_eff, int(cur["eff_start"]))
                hi_end = max(hi_end, int(cur["node_end"]))
                ns_lo = min(ns_lo, int(cur["node_start"]))
                acc += ct
                sids.append(str(cur["symbol_id"]))
                sigs.append(str(cur["signature"]))
                j += 1
                if acc >= merge_target_tokens:
                    break
            if j == i + 1:
                packed_leaves.append(first)
                i += 1
            else:
                packed_leaves.append(
                    {
                        "symbol_id": sids[0],
                        "symbol_ids": sids,
                        "signature": "\n---\n".join(sigs),
                        "kind": str(first["kind"]),
                        "is_container": False,
                        "def_start": int(first["def_start"]),
                        "node_start": ns_lo,
                        "node_end": hi_end,
                        "eff_start": lo_eff,
                        "span_lines": lines[lo_eff:hi_end],
                    }
                )
                i = j
    packed_leaves.sort(key=lambda x: (int(x["eff_start"]), int(x["node_start"])))
    return containers + packed_leaves


def _is_container_symbol_kind(kind: str) -> bool:
    value = (kind or "").lower()
    return any(token in value for token in ("class", "interface", "enum", "record", "type", "object"))


def _is_code_document(relative_path: str, language: str) -> bool:
    suffix = ""
    if "." in relative_path:
        suffix = "." + relative_path.rsplit(".", 1)[-1].lower()
    lang = (language or "").strip().lower()
    return suffix in CODE_FILE_SUFFIXES or lang in CODE_LANGUAGES


def _collect_java_ast_nodes(content: str) -> List[Dict[str, object]]:
    parser = _get_java_parser()
    if parser is None:
        return []
    tree = parser.parse(content.encode("utf-8"))
    out: List[Dict[str, object]] = []
    for node in _walk_tree_sitter(tree.root_node):
        node_type = str(getattr(node, "type", ""))
        if node_type not in JAVA_AST_DECLARATION_TYPES:
            continue
        start_point = getattr(node, "start_point", None)
        end_point = getattr(node, "end_point", None)
        if start_point is None or end_point is None:
            continue
        out.append(
            {
                "node_type": node_type,
                "start_line": int(start_point.row),
                "end_line": int(end_point.row) + 1,
            }
        )
    return out


def _resolve_java_ast_span(
    ast_nodes: Sequence[Dict[str, object]],
    kind: str,
    definition_line: int,
) -> tuple[int, int] | None:
    preferred = _normalize_java_kind(kind)
    candidates = [
        node
        for node in ast_nodes
        if str(node["node_type"]) in preferred
        and int(node["start_line"]) <= definition_line < int(node["end_line"])
    ]
    if not candidates:
        candidates = [
            node for node in ast_nodes if int(node["start_line"]) <= definition_line < int(node["end_line"])
        ]
    if not candidates:
        return None
    best = min(
        candidates,
        key=lambda node: (
            int(node["end_line"]) - int(node["start_line"]),
            abs(int(node["start_line"]) - definition_line),
            int(node["start_line"]),
        ),
    )
    return int(best["start_line"]), int(best["end_line"])


def _fetch_call_context_labels(
    store: SqliteStore,
    repo: str,
    commit: str,
    symbol_id: str,
    max_each: int,
) -> tuple[List[str], List[str]]:
    """从 code_edges（需已 build-code-graph）取 display_name 列表；无表或无边则返回空。"""
    conn = store.conn
    if not _sqlite_table_exists(conn, "code_edges") or not _sqlite_table_exists(conn, "code_nodes"):
        return [], []
    rp = f'%"repo": "{repo}"%'
    cp = f'%"commit": "{commit}"%'
    cur = conn.execute(
        """
        SELECT DISTINCT s.display_name
        FROM code_edges e
        JOIN code_nodes src ON src.node_id = e.src_node
        JOIN code_nodes dst ON dst.node_id = e.dst_node
        JOIN symbols s ON s.symbol_id = dst.symbol_id
        WHERE e.edge_type = 'calls'
          AND src.symbol_id = ?
          AND src.meta_json LIKE ? AND src.meta_json LIKE ?
        ORDER BY s.display_name
        LIMIT ?
        """,
        (symbol_id, rp, cp, max(1, max_each)),
    )
    out = [str(r["display_name"]) for r in cur.fetchall()]
    cur = conn.execute(
        """
        SELECT DISTINCT s.display_name
        FROM code_edges e
        JOIN code_nodes src ON src.node_id = e.src_node
        JOIN code_nodes dst ON dst.node_id = e.dst_node
        JOIN symbols s ON s.symbol_id = src.symbol_id
        WHERE e.edge_type = 'calls'
          AND dst.symbol_id = ?
          AND dst.meta_json LIKE ? AND dst.meta_json LIKE ?
        ORDER BY s.display_name
        LIMIT ?
        """,
        (symbol_id, rp, cp, max(1, max_each)),
    )
    inc = [str(r["display_name"]) for r in cur.fetchall()]
    return out, inc


def _format_call_context_block(outgoing: List[str], incoming: List[str]) -> str:
    # 仅保留“谁调用我”的上下文；“我调用谁”在源码方法体中通常已可直接观察。
    if not incoming:
        return ""
    lines = ["[chunk context]"]
    if incoming:
        lines.append("incoming calls: " + ", ".join(incoming))
    lines.append("[/chunk context]")
    return "\n".join(lines)


def _truncate_text_to_token_budget(
    text: str,
    max_tokens: int,
    count_fn: Callable[[str], int],
) -> str:
    value = str(text or "").strip()
    if not value or max_tokens <= 0:
        return ""
    if count_fn(value) <= max_tokens:
        return value
    truncated = value
    suffix = " ..."
    while truncated and count_fn(truncated + suffix) > max_tokens:
        shrink_by = max(4, len(truncated) // 8)
        truncated = truncated[:-shrink_by].rstrip()
    return (truncated + suffix).strip() if truncated else ""


def _format_chunk_fields_block(
    signature: str,
    incoming: Sequence[str],
    max_tokens: int,
    count_fn: Callable[[str], int],
) -> str:
    if max_tokens <= 0:
        return ""
    remaining = max_tokens
    lines: List[str] = []
    if signature:
        prefix = "signature: "
        budget = remaining - count_fn(prefix)
        value = _truncate_text_to_token_budget(signature, budget, count_fn)
        if value:
            line = prefix + value
            lines.append(line)
            remaining -= count_fn(line)
    if incoming and remaining > 0:
        prefix = "incoming_calls: "
        budget = remaining - count_fn(prefix)
        value = _truncate_text_to_token_budget(", ".join(incoming), budget, count_fn)
        if value:
            line = prefix + value
            lines.append(line)
    if not lines:
        return ""
    return "\n".join(lines)


def _unit_norm(vec: List[float]) -> List[float]:
    norm = math.sqrt(sum(v * v for v in vec))
    if norm == 0:
        return vec
    return [v / norm for v in vec]


class BaseEmbedder:
    def embed(self, text: str) -> List[float]:
        raise NotImplementedError

    def embed_batch(self, texts: Sequence[str]) -> List[List[float]]:
        return [self.embed(t) for t in texts]

    def embed_query(self, text: str) -> List[float]:
        return self.embed(text)

    def embed_documents(self, texts: Sequence[str]) -> List[List[float]]:
        return self.embed_batch(texts)

    def supports_native_batch(self) -> bool:
        return True

    def provider_name(self) -> str:
        return self.__class__.__name__


class DeterministicEmbedder(BaseEmbedder):

    def __init__(self, dim: int = 128) -> None:
        self.dim = dim

    def embed(self, text: str) -> List[float]:
        vec = [0.0] * self.dim
        for token in text.split():
            digest = hashlib.sha1(token.encode("utf-8")).digest()
            idx = int.from_bytes(digest[:4], "big") % self.dim
            sign = 1.0 if digest[4] % 2 == 0 else -1.0
            vec[idx] += sign
        return _unit_norm(vec)


@dataclass
class HttpEmbeddingClient(BaseEmbedder):
    model: str
    api_base: str
    api_key: str = ""
    timeout_s: int = 30
    endpoint: str = "/embeddings"

    def _url(self) -> str:
        base = self.api_base.rstrip("/")
        endpoint = self.endpoint if self.endpoint.startswith("/") else f"/{self.endpoint}"
        return f"{base}{endpoint}"

    def _request(self, payload: Dict[str, object]) -> Dict[str, object]:
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            self._url(),
            data=data,
            method="POST",
            headers={
                "Content-Type": "application/json",
                **({"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}),
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:  # noqa: S310
                body = resp.read().decode("utf-8")
        except Exception as exc:
            raise _classify_embedding_exception(exc) from exc
        obj = json.loads(body)
        if not isinstance(obj, dict):
            raise ValueError("invalid embedding response format")
        return obj

    @staticmethod
    def _extract_vectors(obj: Dict[str, object]) -> List[List[float]]:
        if isinstance(obj.get("embedding"), list):
            emb = obj["embedding"]
            if emb and isinstance(emb[0], list):
                return [[float(x) for x in item] for item in emb]
            return [[float(x) for x in emb]]
        data_field = obj.get("data")
        if isinstance(data_field, list) and data_field:
            out: List[List[float]] = []
            for item in data_field:
                if not isinstance(item, dict):
                    continue
                if isinstance(item.get("embedding"), list):
                    out.append([float(x) for x in item["embedding"]])
                elif isinstance(item.get("vector"), list):
                    out.append([float(x) for x in item["vector"]])
            if out:
                return out
        raise ValueError("invalid embedding response format")

    def embed(self, text: str) -> List[float]:
        payload = {"model": self.model, "input": text}
        vectors = self._extract_vectors(self._request(payload))
        return vectors[0]

    def embed_batch(self, texts: Sequence[str]) -> List[List[float]]:
        payload = {"model": self.model, "input": list(texts)}
        vectors = self._extract_vectors(self._request(payload))
        if len(vectors) != len(texts):
            raise ValueError("embedding batch size mismatch")
        return vectors


@dataclass
class VoyageEmbeddingClient(BaseEmbedder):
    model: str
    api_key: str
    api_base: str = "https://api.voyageai.com"
    timeout_s: int = 30
    input_type: str = "document"

    def embed_batch(self, texts: Sequence[str]) -> List[List[float]]:
        payload = {
            "model": self.model,
            "input": list(texts),
            "input_type": self.input_type,
        }
        req = urllib.request.Request(
            f"{self.api_base.rstrip('/')}/v1/embeddings",
            data=json.dumps(payload).encode("utf-8"),
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:  # noqa: S310
                body = resp.read().decode("utf-8")
        except Exception as exc:
            raise _classify_embedding_exception(exc) from exc
        obj = json.loads(body)
        if isinstance(obj, dict):
            data_field = obj.get("data")
            if isinstance(data_field, list):
                vectors: List[List[float]] = []
                for item in data_field:
                    if isinstance(item, dict) and isinstance(item.get("embedding"), list):
                        vectors.append([float(x) for x in item["embedding"]])
                if len(vectors) == len(texts):
                    return vectors
        raise ValueError("invalid embedding response format")

    def embed(self, text: str) -> List[float]:
        return self.embed_batch([text])[0]


class LocalSentenceTransformerEmbedder(BaseEmbedder):
    def __init__(self, model: str, device: str = "cpu") -> None:
        try:
            from sentence_transformers import SentenceTransformer  # type: ignore
        except Exception as exc:  # pragma: no cover
            raise RuntimeError(
                "请先安装 sentence-transformers 才能使用 embedding.provider=local"
            ) from exc
        self.model = SentenceTransformer(model, device=device)

    def embed_batch(self, texts: Sequence[str]) -> List[List[float]]:
        vecs = self.model.encode(
            list(texts),
            normalize_embeddings=True,
            convert_to_numpy=True,
            show_progress_bar=False,
        )
        return [[float(x) for x in row] for row in vecs]

    def embed(self, text: str) -> List[float]:
        return self.embed_batch([text])[0]


def cosine(a: List[float], b: List[float]) -> float:
    if not a or not b:
        return 0.0
    return sum(x * y for x, y in zip(a, b))


@dataclass
class QueryEmbeddingCacheEntry:
    vector: List[float]
    expires_at: float


class QueryEmbeddingCache:
    def __init__(self, max_size: int, ttl_s: float) -> None:
        self.max_size = max(0, int(max_size))
        self.ttl_s = max(0.0, float(ttl_s))
        self._items: OrderedDict[tuple[str, str], QueryEmbeddingCacheEntry] = OrderedDict()
        self._lock = Lock()

    def get(self, embedding_version: str, query: str) -> List[float] | None:
        if self.max_size <= 0 or self.ttl_s <= 0:
            return None
        key = (embedding_version, query)
        now = time.time()
        with self._lock:
            entry = self._items.get(key)
            if entry is None:
                return None
            if entry.expires_at <= now:
                self._items.pop(key, None)
                return None
            self._items.move_to_end(key)
            return list(entry.vector)

    def put(self, embedding_version: str, query: str, vector: List[float]) -> None:
        if self.max_size <= 0 or self.ttl_s <= 0:
            return
        key = (embedding_version, query)
        expires_at = time.time() + self.ttl_s
        with self._lock:
            self._items[key] = QueryEmbeddingCacheEntry(vector=list(vector), expires_at=expires_at)
            self._items.move_to_end(key)
            while len(self._items) > self.max_size:
                self._items.popitem(last=False)


@dataclass
class EmbeddingRuntimeStats:
    provider_name: str = ""
    provider_requests: int = 0
    provider_successful_requests: int = 0
    provider_failed_requests: int = 0
    provider_retry_attempts: int = 0
    provider_rate_limit_errors: int = 0
    provider_timeout_errors: int = 0
    provider_network_errors: int = 0
    provider_server_errors: int = 0
    provider_http_errors: int = 0
    provider_unknown_errors: int = 0
    provider_batch_fallback_requests: int = 0
    provider_request_ms: int = 0
    query_requests: int = 0
    query_successful_requests: int = 0
    query_failed_requests: int = 0
    query_retry_attempts: int = 0
    query_cache_hits: int = 0
    query_cache_misses: int = 0
    query_fail_open_requests: int = 0
    query_request_ms: int = 0

    def as_dict(self) -> Dict[str, object]:
        cache_total = self.query_cache_hits + self.query_cache_misses
        provider_requests_per_s = 0.0 if self.provider_request_ms <= 0 else round(self.provider_requests * 1000.0 / self.provider_request_ms, 2)
        avg_provider_request_ms = 0.0 if self.provider_requests == 0 else round(self.provider_request_ms / self.provider_requests, 2)
        avg_query_request_ms = 0.0 if self.query_requests == 0 else round(self.query_request_ms / self.query_requests, 2)
        cache_hit_rate = 0.0 if cache_total == 0 else round(self.query_cache_hits / cache_total, 4)
        return {
            "provider_name": self.provider_name,
            "provider_requests": self.provider_requests,
            "provider_successful_requests": self.provider_successful_requests,
            "provider_failed_requests": self.provider_failed_requests,
            "provider_retry_attempts": self.provider_retry_attempts,
            "provider_rate_limit_errors": self.provider_rate_limit_errors,
            "provider_timeout_errors": self.provider_timeout_errors,
            "provider_network_errors": self.provider_network_errors,
            "provider_server_errors": self.provider_server_errors,
            "provider_http_errors": self.provider_http_errors,
            "provider_unknown_errors": self.provider_unknown_errors,
            "provider_batch_fallback_requests": self.provider_batch_fallback_requests,
            "provider_request_ms": self.provider_request_ms,
            "provider_requests_per_s": provider_requests_per_s,
            "avg_provider_request_ms": avg_provider_request_ms,
            "query_requests": self.query_requests,
            "query_successful_requests": self.query_successful_requests,
            "query_failed_requests": self.query_failed_requests,
            "query_retry_attempts": self.query_retry_attempts,
            "query_cache_hits": self.query_cache_hits,
            "query_cache_misses": self.query_cache_misses,
            "query_cache_hit_rate": cache_hit_rate,
            "query_fail_open_requests": self.query_fail_open_requests,
            "query_request_ms": self.query_request_ms,
            "avg_query_request_ms": avg_query_request_ms,
        }


@dataclass
class EmbeddingRunStats:
    total_chunks: int = 0
    skipped_chunks: int = 0
    attempted_chunks: int = 0
    embedded_chunks: int = 0
    successful_batches: int = 0
    failed_batches: int = 0
    failed_chunks: int = 0
    retried_batches: int = 0
    batches_with_retry: int = 0
    provider_requests: int = 0
    provider_successful_requests: int = 0
    provider_failed_requests: int = 0
    provider_retry_attempts: int = 0
    provider_rate_limit_errors: int = 0
    provider_timeout_errors: int = 0
    provider_network_errors: int = 0
    provider_server_errors: int = 0
    provider_http_errors: int = 0
    provider_unknown_errors: int = 0
    provider_batch_fallback_requests: int = 0
    provider_request_ms: int = 0
    elapsed_ms: int = 0
    error_samples: List[str] = field(default_factory=list)

    def as_dict(self) -> Dict[str, object]:
        success_rate = 1.0 if self.attempted_chunks == 0 else self.embedded_chunks / max(1, self.attempted_chunks)
        effective_chunks_per_s = 0.0 if self.elapsed_ms <= 0 else round(self.embedded_chunks * 1000.0 / self.elapsed_ms, 2)
        effective_batches_per_s = 0.0 if self.elapsed_ms <= 0 else round(self.successful_batches * 1000.0 / self.elapsed_ms, 2)
        provider_requests_per_s = 0.0 if self.elapsed_ms <= 0 else round(self.provider_requests * 1000.0 / self.elapsed_ms, 2)
        avg_provider_request_ms = 0.0 if self.provider_requests == 0 else round(self.provider_request_ms / self.provider_requests, 2)
        return {
            "total_chunks": self.total_chunks,
            "skipped_chunks": self.skipped_chunks,
            "attempted_chunks": self.attempted_chunks,
            "embedded_chunks": self.embedded_chunks,
            "successful_batches": self.successful_batches,
            "failed_batches": self.failed_batches,
            "failed_chunks": self.failed_chunks,
            "retried_batches": self.retried_batches,
            "batches_with_retry": self.batches_with_retry,
            "provider_requests": self.provider_requests,
            "provider_successful_requests": self.provider_successful_requests,
            "provider_failed_requests": self.provider_failed_requests,
            "provider_retry_attempts": self.provider_retry_attempts,
            "provider_rate_limit_errors": self.provider_rate_limit_errors,
            "provider_timeout_errors": self.provider_timeout_errors,
            "provider_network_errors": self.provider_network_errors,
            "provider_server_errors": self.provider_server_errors,
            "provider_http_errors": self.provider_http_errors,
            "provider_unknown_errors": self.provider_unknown_errors,
            "provider_batch_fallback_requests": self.provider_batch_fallback_requests,
            "provider_request_ms": self.provider_request_ms,
            "avg_provider_request_ms": avg_provider_request_ms,
            "elapsed_ms": self.elapsed_ms,
            "effective_chunks_per_s": effective_chunks_per_s,
            "effective_batches_per_s": effective_batches_per_s,
            "provider_requests_per_s": provider_requests_per_s,
            "success_rate": round(success_rate, 4),
            "error_samples": self.error_samples,
        }


class EmbeddingPipeline:
    def __init__(
        self,
        store: SqliteStore,
        embedder: BaseEmbedder | None = None,
        batch_size: int = 64,
        max_workers: int = 4,
        max_retries: int = 2,
        retry_backoff_s: float = 0.5,
        vector_search_store: VectorStore | None = None,
        vector_write_stores: Sequence[VectorStore] | None = None,
        provider_max_concurrency: int = 8,
        online_max_concurrency: int = 8,
        online_query_max_retries: int | None = None,
        online_query_cache_size: int = 1024,
        online_query_cache_ttl_s: float = 300.0,
        fail_open_on_query: bool = True,
        retryable_status_codes: Sequence[int] | None = None,
        progress_callback: Callable[[str], None] | None = None,
        stream_fetch_limit: int | None = None,
        stream_commit_every_batches: int | None = None,
        stream_write_buffer_chunks: int | None = None,
        chunk_token_count: Callable[[str], int] | None = None,
    ) -> None:
        self.store = store
        self.embedder = embedder or DeterministicEmbedder()
        self._chunk_token_count = chunk_token_count or heuristic_chunk_token_count
        self.batch_size = max(1, batch_size)
        self.max_workers = max(1, max_workers)
        self.max_retries = max(0, max_retries)
        self.retry_backoff_s = max(0.0, retry_backoff_s)
        self.provider_max_concurrency = max(1, int(provider_max_concurrency))
        self.online_max_concurrency = max(1, int(online_max_concurrency))
        self.online_query_max_retries = (
            self.max_retries if online_query_max_retries is None else max(0, int(online_query_max_retries))
        )
        self.fail_open_on_query = bool(fail_open_on_query)
        self.retryable_status_codes = {int(v) for v in (retryable_status_codes or [])}
        default_vector_store = SqliteVectorStore(store)
        self.vector_search_store = vector_search_store or default_vector_store
        self.vector_write_stores = dedupe_vector_stores(vector_write_stores or [default_vector_store])
        self._provider_semaphore = BoundedSemaphore(self.provider_max_concurrency)
        self._query_semaphore = BoundedSemaphore(self.online_max_concurrency)
        self._runtime_lock = Lock()
        self._runtime_stats = EmbeddingRuntimeStats(provider_name=self.embedder.provider_name())
        self._query_cache = QueryEmbeddingCache(online_query_cache_size, online_query_cache_ttl_s)
        self._progress_callback = progress_callback
        if stream_fetch_limit is None:
            stream_fetch_limit = max(512, self.batch_size * max(4, self.max_workers * 2))
        self.stream_fetch_limit = max(self.batch_size, int(stream_fetch_limit))
        _sceb = stream_commit_every_batches
        if _sceb is None or int(_sceb or 0) <= 0:
            self.stream_commit_every_batches = DEFAULT_STREAM_COMMIT_EVERY_BATCHES
        else:
            self.stream_commit_every_batches = max(1, int(_sceb))
        _swbc = stream_write_buffer_chunks
        self.stream_write_buffer_chunks = max(0, int(_swbc or 0))

    def _report_progress(self, message: str) -> None:
        if self._progress_callback is not None:
            self._progress_callback(message)

    def runtime_stats_snapshot(self) -> Dict[str, object]:
        with self._runtime_lock:
            return dict(self._runtime_stats.as_dict())

    def _record_runtime_provider_result(self, res: Dict[str, object]) -> None:
        with self._runtime_lock:
            self._runtime_stats.provider_requests += int(res.get("provider_requests", 0))
            self._runtime_stats.provider_successful_requests += int(not bool(res.get("failed", False)))
            self._runtime_stats.provider_failed_requests += int(res.get("provider_failed_requests", 0))
            self._runtime_stats.provider_retry_attempts += int(res.get("retries", 0))
            self._runtime_stats.provider_rate_limit_errors += int(res.get("provider_rate_limit_errors", 0))
            self._runtime_stats.provider_timeout_errors += int(res.get("provider_timeout_errors", 0))
            self._runtime_stats.provider_network_errors += int(res.get("provider_network_errors", 0))
            self._runtime_stats.provider_server_errors += int(res.get("provider_server_errors", 0))
            self._runtime_stats.provider_http_errors += int(res.get("provider_http_errors", 0))
            self._runtime_stats.provider_unknown_errors += int(res.get("provider_unknown_errors", 0))
            self._runtime_stats.provider_batch_fallback_requests += int(
                res.get("provider_batch_fallback_requests", 0)
            )
            self._runtime_stats.provider_request_ms += int(res.get("provider_request_ms", 0))

    def _record_query_outcome(
        self,
        *,
        cache_hit: bool,
        success: bool,
        retries: int,
        elapsed_ms: int,
        fail_open: bool = False,
    ) -> None:
        with self._runtime_lock:
            self._runtime_stats.query_requests += 1
            if cache_hit:
                self._runtime_stats.query_cache_hits += 1
            else:
                self._runtime_stats.query_cache_misses += 1
            self._runtime_stats.query_retry_attempts += retries
            self._runtime_stats.query_request_ms += elapsed_ms
            if success:
                self._runtime_stats.query_successful_requests += 1
            else:
                self._runtime_stats.query_failed_requests += 1
            if fail_open:
                self._runtime_stats.query_fail_open_requests += 1

    def _is_retryable_error(self, err: EmbeddingProviderError) -> bool:
        if err.status_code is not None and err.status_code in self.retryable_status_codes:
            return True
        return err.category in {"rate_limit", "timeout", "network", "server_error"}

    def _execute_provider_call(
        self,
        call: Any,
        *,
        max_retries: int,
        batch_fallback: bool = False,
    ) -> Dict[str, object]:
        retries = 0
        provider_requests = 0
        provider_failed_requests = 0
        provider_request_ms = 0
        provider_rate_limit_errors = 0
        provider_timeout_errors = 0
        provider_network_errors = 0
        provider_server_errors = 0
        provider_http_errors = 0
        provider_unknown_errors = 0
        provider_batch_fallback_requests = 0
        while True:
            attempt_started = time.time()
            provider_requests += 1
            if batch_fallback:
                provider_batch_fallback_requests += 1
            try:
                with self._provider_semaphore:
                    value = call()
                provider_request_ms += int((time.time() - attempt_started) * 1000)
                return {
                    "value": value,
                    "retries": retries,
                    "failed": False,
                    "error": "",
                    "exception": None,
                    "provider_requests": provider_requests,
                    "provider_failed_requests": provider_failed_requests,
                    "provider_request_ms": provider_request_ms,
                    "provider_rate_limit_errors": provider_rate_limit_errors,
                    "provider_timeout_errors": provider_timeout_errors,
                    "provider_network_errors": provider_network_errors,
                    "provider_server_errors": provider_server_errors,
                    "provider_http_errors": provider_http_errors,
                    "provider_unknown_errors": provider_unknown_errors,
                    "provider_batch_fallback_requests": provider_batch_fallback_requests,
                }
            except Exception as exc:
                provider_request_ms += int((time.time() - attempt_started) * 1000)
                provider_failed_requests += 1
                classified = _classify_embedding_exception(exc)
                debug_error = _format_exception_debug(exc)
                if classified.category == "rate_limit":
                    provider_rate_limit_errors += 1
                elif classified.category == "timeout":
                    provider_timeout_errors += 1
                elif classified.category == "network":
                    provider_network_errors += 1
                elif classified.category == "server_error":
                    provider_server_errors += 1
                elif classified.category == "http_error":
                    provider_http_errors += 1
                else:
                    provider_unknown_errors += 1
                if retries >= max_retries or not self._is_retryable_error(classified):
                    return {
                        "value": None,
                        "retries": retries,
                        "failed": True,
                        "error": f"{classified.category}: {classified}",
                        "debug_error": debug_error,
                        "exception": classified,
                        "provider_requests": provider_requests,
                        "provider_failed_requests": provider_failed_requests,
                        "provider_request_ms": provider_request_ms,
                        "provider_rate_limit_errors": provider_rate_limit_errors,
                        "provider_timeout_errors": provider_timeout_errors,
                        "provider_network_errors": provider_network_errors,
                        "provider_server_errors": provider_server_errors,
                        "provider_http_errors": provider_http_errors,
                        "provider_unknown_errors": provider_unknown_errors,
                        "provider_batch_fallback_requests": provider_batch_fallback_requests,
                    }
                sleep_s = self.retry_backoff_s * (2**retries)
                if classified.retry_after_s is not None:
                    sleep_s = max(sleep_s, classified.retry_after_s)
                time.sleep(sleep_s)
                retries += 1

    def _query_vector(self, query: str, embedding_version: str) -> List[float] | None:
        started = time.time()
        cached = self._query_cache.get(embedding_version, query)
        if cached is not None:
            self._record_query_outcome(
                cache_hit=True,
                success=True,
                retries=0,
                elapsed_ms=int((time.time() - started) * 1000),
            )
            return cached
        with self._query_semaphore:
            cached = self._query_cache.get(embedding_version, query)
            if cached is not None:
                self._record_query_outcome(
                    cache_hit=True,
                    success=True,
                    retries=0,
                    elapsed_ms=int((time.time() - started) * 1000),
                )
                return cached
            res = self._execute_provider_call(
                lambda: self.embedder.embed_query(query),
                max_retries=self.online_query_max_retries,
                batch_fallback=False,
            )
        self._record_runtime_provider_result(res)
        elapsed_ms = int((time.time() - started) * 1000)
        if bool(res["failed"]):
            self._record_query_outcome(
                cache_hit=False,
                success=False,
                retries=int(res["retries"]),
                elapsed_ms=elapsed_ms,
                fail_open=self.fail_open_on_query,
            )
            if self.fail_open_on_query:
                return None
            exc = res.get("exception")
            if isinstance(exc, Exception):
                raise exc
            raise RuntimeError(str(res["error"]))
        vector = [float(x) for x in list(res["value"] or [])]
        self._query_cache.put(embedding_version, query, vector)
        self._record_query_outcome(
            cache_hit=False,
            success=True,
            retries=int(res["retries"]),
            elapsed_ms=elapsed_ms,
        )
        return vector

    def build_chunks(
        self,
        repo: str,
        commit: str,
        embedding_version: str,
        target_tokens: int = 512,
        overlap_tokens: int = 48,
        include_leading_doc_comment: bool = True,
        include_call_graph_context: bool = True,
        call_context_max_each: int = 8,
        leading_doc_max_lookback_lines: int = 120,
        chunk_strategy: str = "ast",
        java_treesitter_fallback: bool = True,
        java_container_policy: str = "leaf_preferred",
        fallback_to_definition_span: bool = True,
        ast_min_lines: int = 5,
        function_level_only: bool = True,
        ast_parent_min_lines: int = 8,
        ast_parent_min_tokens: int = 100,
        sibling_merge_enabled: bool = True,
        sibling_merge_small_max_tokens: int = 100,
        sibling_merge_target_tokens: int = 260,
        sibling_merge_max_gap_lines: int = 3,
    ) -> int:
        started = time.time()
        rows = self.store.fetch_documents_for_chunking(repo, commit)
        source_docs = len(rows)
        rows = [
            row
            for row in rows
            if _is_code_document(str(row["relative_path"] or ""), str(row["language"] or ""))
        ]
        chunks: List[Chunk] = []
        call_cache: Dict[str, tuple[List[str], List[str]]] = {}
        strategy = (chunk_strategy or "ast").strip().lower()
        if strategy == "scip_ast":
            strategy = "ast"
        min_ast_lines = max(1, int(ast_min_lines))
        total_docs = len(rows)
        self._report_progress(
            "phase=build_chunks.start "
            f"docs={total_docs} "
            f"filtered_non_code_docs={max(0, source_docs - total_docs)} "
            f"strategy={strategy} function_level_only={function_level_only} "
            f"target_tokens={target_tokens} overlap_tokens={overlap_tokens}"
        )
        last_progress_at = time.time()
        progress_every_docs = max(1, min(100, total_docs // 20 if total_docs > 0 else 1))
        processed_docs = 0
        for row in rows:
            processed_docs += 1
            doc_id = row["document_id"]
            relative_path = str(row["relative_path"] or "")
            language = str(row["language"] or "").strip().lower()
            content = str(row["content"] or "")
            lines = row["content"].splitlines()
            if not lines:
                continue
            used_definition_chunks = False
            if strategy == "ast":
                node_defs = self.store.fetch_definition_nodes_for_document(doc_id)
                if node_defs:
                    _smerge_small = max(8, int(sibling_merge_small_max_tokens))
                    doc_chunks = self._build_chunks_from_scip_ast_nodes(
                        doc_id=doc_id,
                        repo=repo,
                        commit=commit,
                        embedding_version=embedding_version,
                        language=language,
                        content=content,
                        lines=lines,
                        node_defs=node_defs,
                        call_cache=call_cache,
                        target_tokens=target_tokens,
                        overlap_tokens=overlap_tokens,
                        include_leading_doc_comment=include_leading_doc_comment,
                        include_call_graph_context=include_call_graph_context,
                        call_context_max_each=call_context_max_each,
                        leading_doc_max_lookback_lines=leading_doc_max_lookback_lines,
                        java_treesitter_fallback=java_treesitter_fallback,
                        java_container_policy=java_container_policy,
                        min_ast_lines=min_ast_lines,
                        function_level_only=function_level_only,
                        ast_parent_min_lines=max(1, int(ast_parent_min_lines)),
                        ast_parent_min_tokens=max(0, int(ast_parent_min_tokens)),
                        sibling_merge_enabled=bool(sibling_merge_enabled),
                        sibling_merge_small_max_tokens=_smerge_small,
                        sibling_merge_target_tokens=max(_smerge_small, int(sibling_merge_target_tokens)),
                        sibling_merge_max_gap_lines=max(0, int(sibling_merge_max_gap_lines)),
                    )
                    if doc_chunks:
                        used_definition_chunks = True
                        chunks.extend(doc_chunks)
            if not used_definition_chunks and (strategy == "definition_span" or fallback_to_definition_span):
                definitions = self.store.fetch_definition_occurrences_for_document(doc_id)
                if definitions:
                    used_definition_chunks = True
                    chunks.extend(
                        self._build_chunks_from_definition_spans(
                            doc_id=doc_id,
                            repo=repo,
                            commit=commit,
                            embedding_version=embedding_version,
                            lines=lines,
                            definitions=definitions,
                            call_cache=call_cache,
                            target_tokens=target_tokens,
                            overlap_tokens=overlap_tokens,
                            include_leading_doc_comment=include_leading_doc_comment,
                            include_call_graph_context=include_call_graph_context,
                            call_context_max_each=call_context_max_each,
                            leading_doc_max_lookback_lines=leading_doc_max_lookback_lines,
                            function_level_only=function_level_only,
                        )
                    )
            if used_definition_chunks:
                continue
            symbols = self.store.fetch_symbol_ids_for_document(doc_id)
            chunks.extend(
                self._chunks_for_span(
                    doc_id=doc_id,
                    embedding_version=embedding_version,
                    base_chunk_id=f"{doc_id}:doc",
                    span_start_line=0,
                    span_lines=lines,
                    primary_symbols=symbols[:20],
                    target_tokens=target_tokens,
                    overlap_tokens=overlap_tokens,
                )
            )
            now = time.time()
            if (
                processed_docs == 1
                or processed_docs == total_docs
                or processed_docs % progress_every_docs == 0
                or (now - last_progress_at) >= 5.0
            ):
                self._report_progress(
                    f"phase=build_chunks.progress docs={processed_docs}/{total_docs} chunks={len(chunks)} path={relative_path}"
                )
                last_progress_at = now
        if chunks:
            self.store.upsert_chunks(chunks)
            self.store.commit()
        self._report_progress(
            f"phase=build_chunks.done docs={total_docs} chunks={len(chunks)} elapsed_ms={int((time.time() - started) * 1000)}"
        )
        return len(chunks)

    def _build_chunks_from_scip_ast_nodes(
        self,
        doc_id: str,
        repo: str,
        commit: str,
        embedding_version: str,
        language: str,
        content: str,
        lines: List[str],
        node_defs: List[object],
        call_cache: Dict[str, tuple[List[str], List[str]]],
        target_tokens: int,
        overlap_tokens: int,
        include_leading_doc_comment: bool,
        include_call_graph_context: bool,
        call_context_max_each: int,
        leading_doc_max_lookback_lines: int,
        java_treesitter_fallback: bool,
        java_container_policy: str,
        min_ast_lines: int,
        function_level_only: bool,
        ast_parent_min_lines: int,
        ast_parent_min_tokens: int,
        sibling_merge_enabled: bool,
        sibling_merge_small_max_tokens: int,
        sibling_merge_target_tokens: int,
        sibling_merge_max_gap_lines: int,
    ) -> List[Chunk]:
        total_lines = len(lines)
        java_ast_nodes: List[Dict[str, object]] | None = None
        candidates: List[Dict[str, object]] = []
        for occ in node_defs:
            kind = str(occ["kind"] or "")
            if not _should_chunk_symbol_kind(kind, function_level_only=function_level_only):
                continue
            symbol_id = str(occ["symbol_id"])
            def_start = max(0, int(occ["range_start_line"]))
            has_explicit_enclosing = bool(int(occ["has_explicit_enclosing_range"] or 0))
            node_start = max(0, int(occ["node_start_line"]))
            node_end = min(total_lines, max(node_start + 1, int(occ["node_end_line"])))
            if language == "java" and java_treesitter_fallback and not has_explicit_enclosing:
                if java_ast_nodes is None:
                    java_ast_nodes = _collect_java_ast_nodes(content)
                resolved = _resolve_java_ast_span(java_ast_nodes, kind, def_start)
                if resolved is not None:
                    node_start, node_end = resolved
            if node_start >= total_lines:
                continue
            if (node_end - node_start) < min_ast_lines:
                continue
            eff_start = node_start
            if include_leading_doc_comment:
                doc_start = _leading_doc_comment_start_line(lines, def_start, leading_doc_max_lookback_lines)
                eff_start = min(node_start, doc_start)
            span_lines = lines[eff_start:node_end]
            if not span_lines:
                continue
            candidates.append(
                {
                    "symbol_id": symbol_id,
                    "signature": str(occ["signature"] or occ["display_name"] or symbol_id),
                    "kind": kind,
                    "is_container": _is_container_symbol_kind(kind),
                    "def_start": def_start,
                    "node_start": node_start,
                    "node_end": node_end,
                    "eff_start": eff_start,
                    "span_lines": span_lines,
                }
            )
        if java_container_policy == "leaf_preferred":
            leaf_ranges = [
                (int(item["node_start"]), int(item["node_end"]))
                for item in candidates
                if not bool(item["is_container"])
            ]
            if leaf_ranges:
                candidates = [
                    item
                    for item in candidates
                    if not bool(item["is_container"])
                    or not any(
                        int(item["node_start"]) <= leaf_start
                        and leaf_end <= int(item["node_end"])
                        and (int(item["node_start"]), int(item["node_end"])) != (leaf_start, leaf_end)
                        for leaf_start, leaf_end in leaf_ranges
                    )
                ]
        pack_candidates = candidates
        if function_level_only and sibling_merge_enabled:
            leaf_sids = [str(c["symbol_id"]) for c in candidates if not bool(c["is_container"])]
            enc_map = (
                self.store.fetch_enclosing_symbols_for_ids(leaf_sids) if leaf_sids else {}
            )
            pack_candidates = _pack_sibling_function_candidates(
                lines,
                candidates,
                enc_map,
                doc_id,
                enabled=True,
                small_max_tokens=sibling_merge_small_max_tokens,
                merge_target_tokens=sibling_merge_target_tokens,
                max_gap_lines=sibling_merge_max_gap_lines,
                token_count=self._chunk_token_count,
            )
        chunks: List[Chunk] = []
        for item in pack_candidates:
            primary_symbols = [str(s) for s in item.get("symbol_ids", [item["symbol_id"]])]  # type: ignore[union-attr]
            symbol_id0 = primary_symbols[0]
            signature = str(item["signature"])
            node_start = int(item["node_start"])
            node_end = int(item["node_end"])
            span_lines = list(item["span_lines"])
            eff_start = int(item["eff_start"])
            incoming_calls: List[str] = []
            if include_call_graph_context:
                seen_ic: set[str] = set()
                cap = max(call_context_max_each, call_context_max_each * min(4, len(primary_symbols)))
                for sid in primary_symbols:
                    if sid not in call_cache:
                        call_cache[sid] = _fetch_call_context_labels(
                            self.store, repo, commit, sid, call_context_max_each
                        )
                    out_l, in_l = call_cache[sid]
                    _ = out_l
                    for label in in_l:
                        if label in seen_ic:
                            continue
                        seen_ic.add(label)
                        incoming_calls.append(label)
                        if len(incoming_calls) >= cap:
                            break
                    if len(incoming_calls) >= cap:
                        break
            merged_pack = len(primary_symbols) > 1
            base_id = (
                f"{doc_id}:ast_pack:{eff_start}-{node_end}:{symbol_id0}"
                if merged_pack
                else f"{doc_id}:ast:{symbol_id0}:{node_start}-{node_end}"
            )
            chunks.extend(
                self._chunks_for_span(
                    doc_id=doc_id,
                    embedding_version=embedding_version,
                    base_chunk_id=base_id,
                    span_start_line=eff_start,
                    span_lines=span_lines,
                    primary_symbols=primary_symbols,
                    target_tokens=target_tokens,
                    overlap_tokens=overlap_tokens,
                    signature=signature,
                    incoming_calls=incoming_calls,
                )
            )
        if function_level_only:
            function_intervals = [(int(x["node_start"]), int(x["node_end"])) for x in pack_candidates]
            merged_fn = _merge_line_intervals(function_intervals)
            container_entries: List[Dict[str, object]] = []
            for occ in node_defs:
                kind = str(occ["kind"] or "")
                if not _is_container_symbol_kind(kind):
                    continue
                symbol_id = str(occ["symbol_id"])
                def_start = max(0, int(occ["range_start_line"]))
                has_explicit_enclosing = bool(int(occ["has_explicit_enclosing_range"] or 0))
                node_start = max(0, int(occ["node_start_line"]))
                node_end = min(total_lines, max(node_start + 1, int(occ["node_end_line"])))
                if language == "java" and java_treesitter_fallback and not has_explicit_enclosing:
                    if java_ast_nodes is None:
                        java_ast_nodes = _collect_java_ast_nodes(content)
                    resolved = _resolve_java_ast_span(java_ast_nodes, kind, def_start)
                    if resolved is not None:
                        node_start, node_end = resolved
                if node_start >= total_lines:
                    continue
                if (node_end - node_start) < min_ast_lines:
                    continue
                container_entries.append(
                    {
                        "symbol_id": symbol_id,
                        "signature": str(occ["signature"] or occ["display_name"] or symbol_id),
                        "node_start": node_start,
                        "node_end": node_end,
                    }
                )
            container_entries.sort(
                key=lambda d: (int(d["node_end"]) - int(d["node_start"]), int(d["node_start"]))
            )
            for cent in container_entries:
                cs = int(cent["node_start"])
                ce = int(cent["node_end"])
                covered: List[tuple[int, int]] = []
                for fs, fe in merged_fn:
                    lo, hi = max(cs, fs), min(ce, fe)
                    if lo < hi:
                        covered.append((lo, hi))
                ds_de = [
                    (int(d["node_start"]), int(d["node_end"]))
                    for d in container_entries
                    if (int(d["node_start"]), int(d["node_end"])) != (cs, ce)
                    and cs <= int(d["node_start"])
                    and int(d["node_end"]) <= ce
                ]
                covered.extend(ds_de)
                merged_cov = _merge_line_intervals(covered)
                for gs, ge in _gaps_in_half_open_range(cs, ce, merged_cov):
                    if (ge - gs) < ast_parent_min_lines:
                        continue
                    span_lines = lines[gs:ge]
                    if not span_lines:
                        continue
                    if ast_parent_min_tokens > 0 and _joined_lines_token_count(
                        span_lines, self._chunk_token_count
                    ) < ast_parent_min_tokens:
                        continue
                    symbol_id = str(cent["symbol_id"])
                    signature = str(cent["signature"])
                    incoming_calls: List[str] = []
                    if include_call_graph_context:
                        if symbol_id not in call_cache:
                            call_cache[symbol_id] = _fetch_call_context_labels(
                                self.store, repo, commit, symbol_id, call_context_max_each
                            )
                        out_l, in_l = call_cache[symbol_id]
                        _ = out_l
                        incoming_calls = in_l
                    chunks.extend(
                        self._chunks_for_span(
                            doc_id=doc_id,
                            embedding_version=embedding_version,
                            base_chunk_id=f"{doc_id}:ast_parent:{symbol_id}:{gs}-{ge}",
                            span_start_line=gs,
                            span_lines=list(span_lines),
                            primary_symbols=[symbol_id],
                            target_tokens=target_tokens,
                            overlap_tokens=overlap_tokens,
                            signature=signature,
                            incoming_calls=incoming_calls,
                        )
                    )
        return chunks

    def _build_chunks_from_definition_spans(
        self,
        doc_id: str,
        repo: str,
        commit: str,
        embedding_version: str,
        lines: List[str],
        definitions: List[object],
        call_cache: Dict[str, tuple[List[str], List[str]]],
        target_tokens: int,
        overlap_tokens: int,
        include_leading_doc_comment: bool,
        include_call_graph_context: bool,
        call_context_max_each: int,
        leading_doc_max_lookback_lines: int,
        function_level_only: bool,
    ) -> List[Chunk]:
        chunks: List[Chunk] = []
        filtered_definitions = [
            occ
            for occ in definitions
            if _should_chunk_symbol_kind(str(occ["kind"] or ""), function_level_only=function_level_only)
        ]
        for def_idx, occ in enumerate(filtered_definitions):
            start_line = max(0, int(occ["range_start_line"]))
            if start_line >= len(lines):
                continue
            if def_idx + 1 < len(filtered_definitions):
                end_line = min(
                    len(lines),
                    max(start_line + 1, int(filtered_definitions[def_idx + 1]["range_start_line"])),
                )
            else:
                end_line = len(lines)
            eff_start = start_line
            if include_leading_doc_comment:
                eff_start = _leading_doc_comment_start_line(
                    lines, start_line, leading_doc_max_lookback_lines
                )
            span_lines = lines[eff_start:end_line]
            if not span_lines:
                continue
            symbol_id = str(occ["symbol_id"])
            signature = str(occ["signature"] or occ["display_name"] or symbol_id)
            incoming_calls: List[str] = []
            if include_call_graph_context:
                if symbol_id not in call_cache:
                    call_cache[symbol_id] = _fetch_call_context_labels(
                        self.store, repo, commit, symbol_id, call_context_max_each
                    )
                out_l, in_l = call_cache[symbol_id]
                _ = out_l
                incoming_calls = in_l
            chunks.extend(
                self._chunks_for_span(
                    doc_id=doc_id,
                    embedding_version=embedding_version,
                    base_chunk_id=f"{doc_id}:fn:{symbol_id}",
                    span_start_line=eff_start,
                    span_lines=span_lines,
                    primary_symbols=[symbol_id],
                    target_tokens=target_tokens,
                    overlap_tokens=overlap_tokens,
                    signature=signature,
                    incoming_calls=incoming_calls,
                )
            )
        return chunks

    def _exclusive_end_under_source_budget(
        self, span_lines: List[str], i: int, source_target: int, n: int
    ) -> int:
        """最大 ``j``（独占）使 ``join(span_lines[i:j])`` 的 token 数不超过 ``source_target``；至少包含一行。"""
        count = self._chunk_token_count
        if i >= n:
            return n
        one = "\n".join(span_lines[i : i + 1])
        if count(one) > source_target:
            return i + 1
        lo, hi = i + 1, n
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if count("\n".join(span_lines[i:mid])) <= source_target:
                lo = mid
            else:
                hi = mid - 1
        return lo

    def _chunks_for_span(
        self,
        doc_id: str,
        embedding_version: str,
        base_chunk_id: str,
        span_start_line: int,
        span_lines: List[str],
        primary_symbols: List[str],
        target_tokens: int,
        overlap_tokens: int,
        signature: str = "",
        incoming_calls: Sequence[str] = (),
    ) -> List[Chunk]:
        target = max(64, target_tokens)
        source_target = max(1, int(target * 0.8))
        metadata_target = max(0, target - source_target)
        overlap = max(0, min(overlap_tokens, source_target // 2))
        total_lines = len(span_lines)
        out: List[Chunk] = []
        part = 0
        i = 0
        count_fn = self._chunk_token_count
        while i < total_lines:
            j = self._exclusive_end_under_source_budget(span_lines, i, source_target, total_lines)
            source_content = "\n".join(span_lines[i:j]).strip()
            if source_content:
                metadata_block = _format_chunk_fields_block(
                    signature, incoming_calls, metadata_target, count_fn
                )
                content = source_content if not metadata_block else metadata_block + "\n\n" + source_content
                out.append(
                    Chunk(
                        chunk_id=f"{base_chunk_id}:p{part}",
                        document_id=doc_id,
                        content=content,
                        primary_symbol_ids=primary_symbols,
                        span_start_line=span_start_line + i,
                        span_end_line=span_start_line + j,
                        embedding_version=embedding_version,
                    )
                )
                part += 1
            if j >= total_lines:
                break
            if overlap == 0:
                i = j
                continue
            k = j
            while k > i:
                if self._chunk_token_count("\n".join(span_lines[k:j])) >= overlap:
                    break
                k -= 1
            i = max(i + 1, k)
        return out

    def _flush_embedding_batch(
        self,
        embedding_version: str,
        vectors: Dict[str, List[float]],
        *,
        commit: bool = True,
    ) -> None:
        if not vectors:
            return
        for vector_store in self.vector_write_stores:
            vector_store.upsert_embeddings(embedding_version, vectors)
        if commit:
            self.store.commit()

    def run(self, embedding_version: str) -> EmbeddingRunStats:
        started = time.time()
        total_chunks = self.store.count_chunks()
        pending = self.store.count_chunks_missing_embeddings(embedding_version)
        stats = EmbeddingRunStats(
            total_chunks=total_chunks,
            skipped_chunks=max(0, total_chunks - pending),
            attempted_chunks=pending,
        )
        if pending == 0:
            self._report_progress(
                f"phase=embed.done total_chunks={total_chunks} pending_chunks=0 embedded_chunks=0 skipped_chunks={stats.skipped_chunks}"
            )
            stats.elapsed_ms = int((time.time() - started) * 1000)
            return stats
        total_batches = (pending + self.batch_size - 1) // self.batch_size
        self._report_progress(
            "phase=embed.start "
            f"provider={self.embedder.provider_name()} "
            f"total_chunks={total_chunks} pending_chunks={pending} skipped_chunks={stats.skipped_chunks} "
            f"batches={total_batches} batch_size={self.batch_size} "
            f"stream_fetch_limit={self.stream_fetch_limit} "
            f"stream_write_buffer_chunks={self.stream_write_buffer_chunks} "
            f"stream_commit_every_batches={self.stream_commit_every_batches}"
        )

        def _embed_one_batch(batch: Sequence[tuple[str, str]]) -> List[tuple[str, List[float]]]:
            texts = [content for _, content in batch]
            vecs = self.embedder.embed_documents(texts)
            if len(vecs) != len(batch):
                raise ValueError("embedding batch size mismatch")
            return [(cid, vec) for (cid, _), vec in zip(batch, vecs)]

        def _embed_with_retry(batch: Sequence[tuple[str, str]]) -> Dict[str, object]:
            res = self._execute_provider_call(
                lambda: _embed_one_batch(batch),
                max_retries=self.max_retries,
                batch_fallback=(len(batch) > 1 and not self.embedder.supports_native_batch()),
            )
            res["batch_size"] = len(batch)
            res["vectors"] = [] if bool(res["failed"]) else list(res.get("value") or [])
            return res

        def _merge_batch_stats(stats: EmbeddingRunStats, res: Dict[str, object]) -> None:
            retries = int(res["retries"])
            stats.retried_batches += retries
            if retries > 0:
                stats.batches_with_retry += 1
            stats.provider_requests += int(res.get("provider_requests", 0))
            stats.provider_successful_requests += int(not bool(res["failed"]))
            stats.provider_failed_requests += int(res.get("provider_failed_requests", 0))
            stats.provider_retry_attempts += retries
            stats.provider_rate_limit_errors += int(res.get("provider_rate_limit_errors", 0))
            stats.provider_timeout_errors += int(res.get("provider_timeout_errors", 0))
            stats.provider_network_errors += int(res.get("provider_network_errors", 0))
            stats.provider_server_errors += int(res.get("provider_server_errors", 0))
            stats.provider_http_errors += int(res.get("provider_http_errors", 0))
            stats.provider_unknown_errors += int(res.get("provider_unknown_errors", 0))
            stats.provider_batch_fallback_requests += int(res.get("provider_batch_fallback_requests", 0))
            stats.provider_request_ms += int(res.get("provider_request_ms", 0))
            self._record_runtime_provider_result(res)

        processed_batches = 0
        embedded_chunks = 0
        flushes_since_sqlite_commit = 0
        pending_write: Dict[str, List[float]] = {}
        last_progress_at = time.time()
        progress_every_batches = max(1, min(50, total_batches // 20 if total_batches > 0 else 1))

        def _emit_embed_progress(force: bool = False) -> None:
            nonlocal last_progress_at
            now = time.time()
            if not force and processed_batches not in {1, total_batches}:
                if processed_batches % progress_every_batches != 0 and (now - last_progress_at) < 5.0:
                    return
            self._report_progress(
                "phase=embed.progress "
                f"batches={processed_batches}/{total_batches} "
                f"embedded_chunks={embedded_chunks} "
                f"failed_batches={stats.failed_batches} "
                f"retried_batches={stats.retried_batches} "
                f"provider_requests={stats.provider_requests}"
            )
            last_progress_at = now

        def _flush_after_vector_write(vectors: Dict[str, List[float]]) -> None:
            nonlocal flushes_since_sqlite_commit
            if not vectors:
                return
            flushes_since_sqlite_commit += 1
            do_commit = flushes_since_sqlite_commit >= self.stream_commit_every_batches
            self._flush_embedding_batch(embedding_version, vectors, commit=do_commit)
            if do_commit:
                flushes_since_sqlite_commit = 0

        def _drain_write_buffer(*, force: bool = False) -> None:
            """将 pending_write 中达到 stream_write_buffer_chunks 的向量写出；force 时写出剩余全部。"""
            nonlocal pending_write
            limit = self.stream_write_buffer_chunks
            if limit <= 0:
                return
            while True:
                n = len(pending_write)
                if n == 0:
                    break
                if not force and n < limit:
                    break
                if force:
                    chunk = dict(pending_write)
                    pending_write.clear()
                else:
                    keys = list(pending_write.keys())[:limit]
                    chunk = {k: pending_write.pop(k) for k in keys}
                _flush_after_vector_write(chunk)

        def _handle_result(res: Dict[str, object]) -> None:
            nonlocal processed_batches, embedded_chunks, flushes_since_sqlite_commit, pending_write
            processed_batches += 1
            _merge_batch_stats(stats, res)
            if bool(res["failed"]):
                stats.failed_batches += 1
                stats.failed_chunks += int(res["batch_size"])
                if len(stats.error_samples) < 5 and str(res["error"]):
                    stats.error_samples.append(str(res["error"]))
                self._report_progress(
                    f"phase=embed.batch_failed batch={processed_batches}/{total_batches} size={res['batch_size']} error={res['error']}"
                )
                if str(res.get("debug_error") or "").strip():
                    self._report_progress(
                        f"phase=embed.batch_failed_debug batch={processed_batches}/{total_batches} {res['debug_error']}"
                    )
                _emit_embed_progress()
                return
            stats.successful_batches += 1
            batch_vecs = {cid: vec for cid, vec in res["vectors"]}
            embedded_chunks += len(batch_vecs)
            if self.stream_write_buffer_chunks <= 0:
                _flush_after_vector_write(batch_vecs)
            else:
                pending_write.update(batch_vecs)
                _drain_write_buffer(force=False)
            if int(res["retries"]) > 0:
                self._report_progress(
                    f"phase=embed.batch_retried batch={processed_batches}/{total_batches} retries={res['retries']}"
                )
            _emit_embed_progress()

        after_chunk_id: str | None = None
        while True:
            page = self.store.fetch_chunks_missing_embeddings_page(
                embedding_version, after_chunk_id, self.stream_fetch_limit
            )
            if not page:
                break
            after_chunk_id = str(page[-1]["chunk_id"])
            items = [(str(r["chunk_id"]), str(r["content"])) for r in page]
            batches = [items[i : i + self.batch_size] for i in range(0, len(items), self.batch_size)]
            if self.max_workers == 1:
                for batch in batches:
                    _handle_result(_embed_with_retry(batch))
            else:
                with ThreadPoolExecutor(max_workers=self.max_workers) as pool:
                    futures = [pool.submit(_embed_with_retry, b) for b in batches]
                    for fut in as_completed(futures):
                        _handle_result(fut.result())

        if self.stream_write_buffer_chunks > 0:
            _drain_write_buffer(force=True)
        if flushes_since_sqlite_commit > 0:
            self.store.commit()

        stats.embedded_chunks = embedded_chunks
        stats.elapsed_ms = int((time.time() - started) * 1000)
        self._report_progress(
            "phase=embed.done "
            f"batches={total_batches} "
            f"embedded_chunks={stats.embedded_chunks} "
            f"failed_batches={stats.failed_batches} "
            f"retried_batches={stats.retried_batches} "
            f"elapsed_ms={stats.elapsed_ms}"
        )
        return stats

    def semantic_search(self, query: str, embedding_version: str, top_k: int) -> List[tuple[str, float]]:
        qv = self._query_vector(query, embedding_version)
        if qv is None:
            return []
        hits = self.vector_search_store.search(qv, embedding_version, top_k)
        return [(h.chunk_id, h.score) for h in hits]
