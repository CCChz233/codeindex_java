from __future__ import annotations

import json
import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, Sequence

from .models import QueryResult
from .spring_semantic_eval import sample_relevant_chunk_ids
from .storage import SqliteStore


class DenseSearchPipeline(Protocol):
    def semantic_search(
        self,
        query: str,
        embedding_version: str,
        top_k: int,
    ) -> list[tuple[str, float]]:
        ...


@dataclass(frozen=True)
class RetrievalCompareCase:
    case_id: str
    query: str
    gold_files: tuple[str, ...]
    gold_symbols: tuple[str, ...]
    gold_chunks: tuple[str, ...]
    repo_sha: str
    raw: dict[str, Any]
    line: int | None = None


@dataclass(frozen=True)
class RelevantChunks:
    chunk_ids: frozenset[str]
    by_file: dict[str, frozenset[str]]
    by_symbol: dict[str, frozenset[str]]
    direct_chunks: frozenset[str]


@dataclass(frozen=True)
class _RetrieverCaseResult:
    metrics: dict[str, float]
    retrieved: list[dict[str, Any]]
    failure_reason: str
    error: dict[str, str] | None = None


def _as_str_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        raw = value.split("|") if "|" in value else [value]
    elif isinstance(value, Iterable) and not isinstance(value, (bytes, bytearray, dict)):
        raw = list(value)
    else:
        raw = [value]
    return [str(item).strip() for item in raw if str(item).strip()]


def _normalize_path(value: str) -> str:
    return str(value or "").replace("\\", "/").lstrip("./")


def _path_matches(path: str, expected_file: str) -> bool:
    actual = _normalize_path(path)
    expected = _normalize_path(expected_file)
    if not actual or not expected:
        return False
    return actual == expected or actual.endswith("/" + expected) or expected.endswith("/" + actual)


def _get_nested_dict(row: dict[str, Any], key: str) -> dict[str, Any]:
    value = row.get(key)
    return value if isinstance(value, dict) else {}


def _first_list(*values: Any) -> list[str]:
    for value in values:
        items = _as_str_list(value)
        if items:
            return items
    return []


def _normalize_case(row: dict[str, Any], line_no: int | None, idx: int) -> RetrievalCompareCase:
    ground_truth = _get_nested_dict(row, "ground_truth")
    expected = _get_nested_dict(row, "expected")
    query = str(row.get("query") or row.get("text") or "").strip()
    if not query:
        loc = f"line {line_no}" if line_no is not None else f"sample {idx}"
        raise ValueError(f"{loc}: retrieval compare case requires non-empty query")

    gold_files = _first_list(
        row.get("gold_files"),
        ground_truth.get("gold_files"),
        ground_truth.get("files"),
        expected.get("files"),
        expected.get("paths"),
    )
    gold_symbols = _first_list(
        row.get("gold_symbols"),
        ground_truth.get("gold_symbols"),
        ground_truth.get("symbols"),
        expected.get("symbols"),
        expected.get("symbol_ids"),
    )
    gold_chunks = _first_list(
        row.get("gold_chunks"),
        row.get("chunk_ids"),
        ground_truth.get("gold_chunks"),
        ground_truth.get("chunks"),
        ground_truth.get("chunk_ids"),
        expected.get("chunks"),
        expected.get("chunk_ids"),
    )
    if not gold_files and not gold_symbols and not gold_chunks:
        loc = f"line {line_no}" if line_no is not None else f"sample {idx}"
        raise ValueError(f"{loc}: retrieval compare case requires gold files, symbols, or chunks")

    case_id = str(row.get("id") or row.get("sample_id") or row.get("case_id") or f"case-{idx}")
    repo_sha = str(row.get("repo_sha") or row.get("commit") or row.get("commit_hash") or "").strip()
    return RetrievalCompareCase(
        case_id=case_id,
        query=query,
        gold_files=tuple(_normalize_path(x) for x in gold_files),
        gold_symbols=tuple(gold_symbols),
        gold_chunks=tuple(gold_chunks),
        repo_sha=repo_sha,
        raw=row,
        line=line_no,
    )


def load_retrieval_compare_cases(dataset_path: str) -> list[RetrievalCompareCase]:
    path = Path(dataset_path)
    if not path.is_file():
        raise FileNotFoundError(f"dataset not found: {dataset_path}")

    rows: list[tuple[dict[str, Any], int | None]] = []
    if path.suffix.lower() == ".jsonl":
        with path.open("r", encoding="utf-8") as f:
            for line_no, line in enumerate(f, start=1):
                raw = line.strip()
                if not raw:
                    continue
                try:
                    row = json.loads(raw)
                except json.JSONDecodeError as exc:
                    raise ValueError(f"invalid JSON on line {line_no}: {exc}") from exc
                if not isinstance(row, dict):
                    raise ValueError(f"dataset line {line_no} must be a JSON object")
                rows.append((row, line_no))
    else:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            samples = data.get("samples")
            if not isinstance(samples, list):
                raise ValueError("JSON dataset object must contain samples[]")
        elif isinstance(data, list):
            samples = data
        else:
            raise ValueError("dataset must be JSONL, a JSON array, or a JSON object with samples[]")
        for row in samples:
            if not isinstance(row, dict):
                raise ValueError("dataset samples must be JSON objects")
            rows.append((row, None))

    return [_normalize_case(row, line_no, idx) for idx, (row, line_no) in enumerate(rows)]


def _chunk_ids_for_file(
    store: SqliteStore,
    repo: str,
    commit: str,
    expected_file: str,
) -> set[str]:
    normalized = _normalize_path(expected_file)
    if not normalized:
        return set()
    cur = store.conn.execute(
        """
        SELECT document_id
        FROM documents
        WHERE repo = ?
          AND commit_hash = ?
          AND (relative_path = ? OR relative_path LIKE ?)
        """,
        (repo, commit, normalized, f"%/{normalized}"),
    )
    doc_ids = [str(row["document_id"]) for row in cur.fetchall()]
    out: set[str] = set()
    for doc_id in doc_ids:
        cur2 = store.conn.execute(
            "SELECT chunk_id FROM chunks WHERE document_id = ?",
            (doc_id,),
        )
        out.update(str(row["chunk_id"]) for row in cur2.fetchall())
    return out


def _direct_chunk_ids_for_symbol(
    store: SqliteStore,
    repo: str,
    commit: str,
    symbol: str,
) -> set[str]:
    symbol = str(symbol or "").strip()
    if not symbol:
        return set()
    cur = store.conn.execute(
        """
        SELECT c.chunk_id, c.primary_symbol_ids
        FROM chunks c
        JOIN documents d ON d.document_id = c.document_id
        WHERE d.repo = ?
          AND d.commit_hash = ?
          AND (c.primary_symbol_ids LIKE ? OR c.chunk_id LIKE ?)
        """,
        (repo, commit, f"%{symbol}%", f"%{symbol}%"),
    )
    out: set[str] = set()
    for row in cur.fetchall():
        chunk_id = str(row["chunk_id"])
        primary_raw = str(row["primary_symbol_ids"] or "")
        if _symbol_matches(chunk_id, _decode_primary_symbols(primary_raw), symbol):
            out.add(chunk_id)
    return out


def _relevant_chunks_for_case(
    store: SqliteStore,
    repo: str,
    commit: str,
    case: RetrievalCompareCase,
) -> RelevantChunks:
    by_file: dict[str, frozenset[str]] = {}
    by_symbol: dict[str, frozenset[str]] = {}
    chunk_ids: set[str] = set(case.gold_chunks)
    for path in case.gold_files:
        hits = _chunk_ids_for_file(store, repo, commit, path)
        by_file[path] = frozenset(hits)
        chunk_ids.update(hits)
    for symbol in case.gold_symbols:
        hits = set(sample_relevant_chunk_ids(store, repo, commit, [symbol]))
        hits.update(_direct_chunk_ids_for_symbol(store, repo, commit, symbol))
        by_symbol[symbol] = frozenset(hits)
        chunk_ids.update(hits)
    return RelevantChunks(
        chunk_ids=frozenset(chunk_ids),
        by_file=by_file,
        by_symbol=by_symbol,
        direct_chunks=frozenset(case.gold_chunks),
    )


def _decode_primary_symbols(raw: str) -> list[str]:
    try:
        value = json.loads(raw or "[]")
    except Exception:
        return []
    if isinstance(value, list):
        return [str(item) for item in value]
    return []


def _symbol_matches(chunk_id: str, primary_symbols: Sequence[str], expected_symbol: str) -> bool:
    expected = str(expected_symbol or "").strip().replace("`", "")
    if not expected:
        return False
    method_hint = ""
    if "#" in expected:
        method_hint = expected.split("#", 1)[1].split("/", 1)[0].split("(", 1)[0].strip()
    haystacks = [chunk_id, *primary_symbols]
    for haystack in haystacks:
        h = str(haystack or "").strip()
        if not h:
            continue
        if expected == h or expected in h or h in expected:
            return True
        if method_hint and method_hint in h:
            return True
    return False


def _chunk_meta_row(
    store: SqliteStore,
    chunk_id: str,
    score: float,
    rank: int,
    relevant: RelevantChunks,
    case: RetrievalCompareCase,
) -> dict[str, Any]:
    meta = store.fetch_chunk_metadata(chunk_id, include_content=False) or {}
    primary_symbols = store.fetch_chunk_primary_symbols(chunk_id)
    path = str(meta.get("path") or "")
    matched_files = [f for f in case.gold_files if _path_matches(path, f)]
    matched_symbols = [
        symbol
        for symbol, chunk_ids in relevant.by_symbol.items()
        if chunk_id in chunk_ids or _symbol_matches(chunk_id, primary_symbols, symbol)
    ]
    matched_chunks = [chunk_id] if chunk_id in relevant.direct_chunks else []
    return {
        "rank": rank,
        "chunk_id": chunk_id,
        "score": float(score),
        "path": path,
        "start_line": meta.get("start_line"),
        "end_line": meta.get("end_line"),
        "language": meta.get("language"),
        "primary_symbols": primary_symbols,
        "is_relevant": chunk_id in relevant.chunk_ids,
        "matched_expected_files": matched_files,
        "matched_expected_symbols": matched_symbols,
        "matched_expected_chunks": matched_chunks,
    }


def _dcg(relevances: Sequence[int]) -> float:
    return sum((2**rel - 1) / math.log2(rank + 1) for rank, rel in enumerate(relevances, start=1))


def _ratio(num: int, denom: int) -> float:
    return float(num) / float(denom) if denom > 0 else 0.0


def _target_recall(*values: tuple[bool, float]) -> float:
    active = [value for enabled, value in values if enabled]
    return sum(active) / len(active) if active else 0.0


def _metrics_for_rows(
    rows: Sequence[dict[str, Any]],
    case: RetrievalCompareCase,
    relevant: RelevantChunks,
    top_ks: Sequence[int],
) -> dict[str, float]:
    metrics: dict[str, float] = {}
    relevant_count = len(relevant.chunk_ids)
    gold_file_count = len(case.gold_files)
    gold_symbol_count = len(case.gold_symbols)
    direct_chunk_count = len(case.gold_chunks)
    for k in top_ks:
        top_rows = list(rows[:k])
        seen_relevant_chunks: set[str] = set()
        seen_files: set[str] = set()
        seen_symbols: set[str] = set()
        seen_direct_chunks: set[str] = set()
        gains: list[int] = []
        first_rank = 0
        relevant_rows = 0
        for rank, row in enumerate(top_rows, start=1):
            chunk_id = str(row.get("chunk_id") or "")
            is_relevant = bool(row.get("is_relevant"))
            gains.append(1 if is_relevant else 0)
            if is_relevant:
                relevant_rows += 1
                if chunk_id:
                    seen_relevant_chunks.add(chunk_id)
            if is_relevant and first_rank == 0:
                first_rank = rank
            seen_files.update(str(x) for x in row.get("matched_expected_files", []) if str(x))
            seen_symbols.update(str(x) for x in row.get("matched_expected_symbols", []) if str(x))
            seen_direct_chunks.update(str(x) for x in row.get("matched_expected_chunks", []) if str(x))

        ideal_hits = min(relevant_count, k)
        idcg = _dcg([1] * ideal_hits)
        hit = 1.0 if seen_relevant_chunks else 0.0
        file_recall = _ratio(len(seen_files), gold_file_count)
        symbol_recall = _ratio(len(seen_symbols), gold_symbol_count)
        direct_chunk_recall = _ratio(len(seen_direct_chunks), direct_chunk_count)
        metrics[f"hit@{k}"] = hit
        # Backward-compatible alias. Historically this field meant Hit@K, not strict recall.
        metrics[f"recall@{k}"] = hit
        metrics[f"mrr@{k}"] = (1.0 / first_rank) if first_rank else 0.0
        metrics[f"precision@{k}"] = _ratio(relevant_rows, k)
        metrics[f"chunk_recall@{k}"] = (
            _ratio(len(seen_relevant_chunks), relevant_count)
        )
        metrics[f"file_hit@{k}"] = 1.0 if seen_files else 0.0
        metrics[f"file_recall@{k}"] = file_recall
        metrics[f"symbol_hit@{k}"] = 1.0 if seen_symbols else 0.0
        metrics[f"symbol_recall@{k}"] = symbol_recall
        metrics[f"direct_chunk_recall@{k}"] = direct_chunk_recall
        metrics[f"target_recall@{k}"] = _target_recall(
            (gold_file_count > 0, file_recall),
            (gold_symbol_count > 0, symbol_recall),
            (direct_chunk_count > 0, direct_chunk_recall),
        )
        metrics[f"ndcg@{k}"] = (_dcg(gains[:k]) / idcg) if idcg > 0 else 0.0
    return metrics


def _oracle_union_metrics(
    dense: _RetrieverCaseResult,
    bm25: _RetrieverCaseResult,
    case: RetrievalCompareCase,
    relevant: RelevantChunks,
    top_ks: Sequence[int],
) -> dict[str, float]:
    metrics: dict[str, float] = {}
    relevant_count = len(relevant.chunk_ids)
    gold_file_count = len(case.gold_files)
    gold_symbol_count = len(case.gold_symbols)
    direct_chunk_count = len(case.gold_chunks)
    for k in top_ks:
        top_rows = [*dense.retrieved[:k], *bm25.retrieved[:k]]
        seen_relevant_chunks: set[str] = set()
        seen_files: set[str] = set()
        seen_symbols: set[str] = set()
        seen_direct_chunks: set[str] = set()
        for row in top_rows:
            chunk_id = str(row.get("chunk_id") or "")
            if bool(row.get("is_relevant")) and chunk_id:
                seen_relevant_chunks.add(chunk_id)
            seen_files.update(str(x) for x in row.get("matched_expected_files", []) if str(x))
            seen_symbols.update(str(x) for x in row.get("matched_expected_symbols", []) if str(x))
            seen_direct_chunks.update(str(x) for x in row.get("matched_expected_chunks", []) if str(x))
        file_recall = _ratio(len(seen_files), gold_file_count)
        symbol_recall = _ratio(len(seen_symbols), gold_symbol_count)
        direct_chunk_recall = _ratio(len(seen_direct_chunks), direct_chunk_count)
        hit = 1.0 if seen_relevant_chunks else 0.0
        metrics[f"hit@{k}"] = hit
        metrics[f"recall@{k}"] = hit
        metrics[f"mrr@{k}"] = max(
            dense.metrics.get(f"mrr@{k}", 0.0),
            bm25.metrics.get(f"mrr@{k}", 0.0),
        )
        returned_ids = {str(r.get("chunk_id") or "") for r in top_rows if str(r.get("chunk_id") or "")}
        metrics[f"precision@{k}"] = _ratio(len(seen_relevant_chunks), max(1, len(returned_ids)))
        metrics[f"chunk_recall@{k}"] = _ratio(len(seen_relevant_chunks), relevant_count)
        metrics[f"file_hit@{k}"] = 1.0 if seen_files else 0.0
        metrics[f"file_recall@{k}"] = file_recall
        metrics[f"symbol_hit@{k}"] = 1.0 if seen_symbols else 0.0
        metrics[f"symbol_recall@{k}"] = symbol_recall
        metrics[f"direct_chunk_recall@{k}"] = direct_chunk_recall
        metrics[f"target_recall@{k}"] = _target_recall(
            (gold_file_count > 0, file_recall),
            (gold_symbol_count > 0, symbol_recall),
            (direct_chunk_count > 0, direct_chunk_recall),
        )
        metrics[f"ndcg@{k}"] = max(
            dense.metrics.get(f"ndcg@{k}", 0.0),
            bm25.metrics.get(f"ndcg@{k}", 0.0),
        )
    return metrics


def _failure_reason(
    *,
    error: dict[str, str] | None,
    relevant_count: int,
    returned_count: int,
    metrics: dict[str, float],
    max_k: int,
) -> str:
    if error is not None:
        return "case_error"
    if relevant_count <= 0:
        return "empty_relevant"
    if returned_count <= 0:
        return "no_results"
    if metrics.get(f"hit@{max_k}", metrics.get(f"recall@{max_k}", 0.0)) <= 0:
        return "no_relevant_hit"
    return ""


def _run_dense(
    store: SqliteStore,
    pipeline: DenseSearchPipeline,
    case: RetrievalCompareCase,
    relevant: RelevantChunks,
    embedding_version: str,
    top_ks: Sequence[int],
) -> _RetrieverCaseResult:
    max_k = max(top_ks)
    error: dict[str, str] | None = None
    hits: list[tuple[str, float]]
    try:
        hits = pipeline.semantic_search(case.query, embedding_version, max_k)
    except Exception as exc:
        hits = []
        error = {"type": type(exc).__name__, "message": str(exc)}
    retrieved = [
        _chunk_meta_row(store, chunk_id, score, rank, relevant, case)
        for rank, (chunk_id, score) in enumerate(hits, start=1)
    ]
    metrics = _metrics_for_rows(retrieved, case, relevant, top_ks)
    return _RetrieverCaseResult(
        metrics=metrics,
        retrieved=retrieved,
        failure_reason=_failure_reason(
            error=error,
            relevant_count=len(relevant.chunk_ids),
            returned_count=len(hits),
            metrics=metrics,
            max_k=max_k,
        ),
        error=error,
    )


def _run_bm25(
    store: SqliteStore,
    case: RetrievalCompareCase,
    relevant: RelevantChunks,
    top_ks: Sequence[int],
) -> _RetrieverCaseResult:
    max_k = max(top_ks)
    error: dict[str, str] | None = None
    results: list[QueryResult]
    try:
        results = store.keyword_search(case.query, max_k)
    except Exception as exc:
        results = []
        error = {"type": type(exc).__name__, "message": str(exc)}
    retrieved = [
        _chunk_meta_row(store, result.result_id, result.score, rank, relevant, case)
        for rank, result in enumerate(results, start=1)
    ]
    metrics = _metrics_for_rows(retrieved, case, relevant, top_ks)
    return _RetrieverCaseResult(
        metrics=metrics,
        retrieved=retrieved,
        failure_reason=_failure_reason(
            error=error,
            relevant_count=len(relevant.chunk_ids),
            returned_count=len(results),
            metrics=metrics,
            max_k=max_k,
        ),
        error=error,
    )


def _run_rrf_fusion(
    dense: _RetrieverCaseResult,
    bm25: _RetrieverCaseResult,
    case: RetrievalCompareCase,
    relevant: RelevantChunks,
    top_ks: Sequence[int],
    *,
    rrf_k: int = 60,
) -> _RetrieverCaseResult:
    max_k = max(top_ks)
    by_chunk: dict[str, dict[str, Any]] = {}
    scores: dict[str, float] = {}
    for rows in (dense.retrieved, bm25.retrieved):
        for row in rows:
            chunk_id = str(row.get("chunk_id") or "")
            rank = int(row.get("rank") or 0)
            if not chunk_id or rank <= 0:
                continue
            scores[chunk_id] = scores.get(chunk_id, 0.0) + 1.0 / (rrf_k + rank)
            by_chunk.setdefault(chunk_id, dict(row))
    ranked_ids = sorted(scores.keys(), key=lambda cid: (-scores[cid], cid))[:max_k]
    retrieved: list[dict[str, Any]] = []
    for rank, chunk_id in enumerate(ranked_ids, start=1):
        row = dict(by_chunk[chunk_id])
        row["rank"] = rank
        row["score"] = scores[chunk_id]
        row["fusion"] = "rrf"
        retrieved.append(row)
    metrics = _metrics_for_rows(retrieved, case, relevant, top_ks)
    return _RetrieverCaseResult(
        metrics=metrics,
        retrieved=retrieved,
        failure_reason=_failure_reason(
            error=None,
            relevant_count=len(relevant.chunk_ids),
            returned_count=len(retrieved),
            metrics=metrics,
            max_k=max_k,
        ),
    )


def _run_dense_guarded_fusion(
    dense: _RetrieverCaseResult,
    bm25: _RetrieverCaseResult,
    case: RetrievalCompareCase,
    relevant: RelevantChunks,
    top_ks: Sequence[int],
) -> _RetrieverCaseResult:
    max_k = max(top_ks)
    dense_rows = [dict(row) for row in dense.retrieved[: max_k * 2]]
    bm25_rows = [dict(row) for row in bm25.retrieved[: max_k * 2]]
    if not dense_rows:
        metrics = _metrics_for_rows(bm25_rows[:max_k], case, relevant, top_ks)
        return _RetrieverCaseResult(
            metrics=metrics,
            retrieved=bm25_rows[:max_k],
            failure_reason=_failure_reason(
                error=None,
                relevant_count=len(relevant.chunk_ids),
                returned_count=len(bm25_rows),
                metrics=metrics,
                max_k=max_k,
            ),
        )

    dense_ids = {str(row.get("chunk_id") or "") for row in dense_rows if str(row.get("chunk_id") or "")}
    bm25_rank = {
        str(row.get("chunk_id") or ""): int(row.get("rank") or rank)
        for rank, row in enumerate(bm25_rows, start=1)
        if str(row.get("chunk_id") or "")
    }
    for rank, row in enumerate(dense_rows, start=1):
        chunk_id = str(row.get("chunk_id") or "")
        row["rank"] = rank
        row["fusion"] = "dense_guarded"
        row["dense_rank"] = rank
        if chunk_id in bm25_rank:
            row["bm25_rank"] = bm25_rank[chunk_id]

    max_inserts = min(2, max(0, max_k // 10))
    inserts: list[dict[str, Any]] = []
    if max_inserts > 0:
        dense_top_paths = {
            str(row.get("path") or "")
            for row in dense_rows[:max_k]
            if str(row.get("path") or "")
        }
        rank_cap = max(1, min(3, max_k // 3))
        for rank, row in enumerate(bm25_rows, start=1):
            if rank > rank_cap:
                break
            chunk_id = str(row.get("chunk_id") or "")
            if not chunk_id or chunk_id in dense_ids:
                continue
            path = str(row.get("path") or "")
            if path and path in dense_top_paths:
                continue
            candidate = dict(row)
            candidate["fusion"] = "dense_guarded"
            candidate["guarded_insert"] = True
            candidate["bm25_rank"] = rank
            inserts.append(candidate)
            if len(inserts) >= max_inserts:
                break

    keep_n = max(1, max_k - len(inserts))
    fused = [*dense_rows[:keep_n], *inserts, *dense_rows[keep_n:]]
    retrieved: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in fused:
        chunk_id = str(row.get("chunk_id") or "")
        if not chunk_id or chunk_id in seen:
            continue
        seen.add(chunk_id)
        out_row = dict(row)
        out_row["rank"] = len(retrieved) + 1
        retrieved.append(out_row)
        if len(retrieved) >= max_k:
            break

    metrics = _metrics_for_rows(retrieved, case, relevant, top_ks)
    return _RetrieverCaseResult(
        metrics=metrics,
        retrieved=retrieved,
        failure_reason=_failure_reason(
            error=None,
            relevant_count=len(relevant.chunk_ids),
            returned_count=len(retrieved),
            metrics=metrics,
            max_k=max_k,
        ),
    )


def _summarize_retriever(
    case_results: Sequence[_RetrieverCaseResult],
    relevant_counts: Sequence[int],
    top_ks: Sequence[int],
) -> dict[str, Any]:
    n = len(case_results)
    out: dict[str, Any] = {
        "cases": n,
        "empty_relevant_count": sum(1 for count in relevant_counts if count <= 0),
        "no_results_count": sum(
            1
            for result, count in zip(case_results, relevant_counts)
            if count > 0 and not result.retrieved and result.error is None
        ),
        "case_error_count": sum(1 for result in case_results if result.error is not None),
    }
    metric_names = (
        "hit",
        "recall",
        "mrr",
        "precision",
        "ndcg",
        "chunk_recall",
        "file_hit",
        "file_recall",
        "symbol_hit",
        "symbol_recall",
        "direct_chunk_recall",
        "target_recall",
    )
    for k in top_ks:
        for metric_name in metric_names:
            key = f"{metric_name}@{k}"
            out[key] = (
                0.0
                if n == 0
                else round(sum(result.metrics.get(key, 0.0) for result in case_results) / n, 6)
            )
    return out


_GROUP_BY_FIELDS = ("source_type", "semantic_scope", "structure_status", "query_source")


def _group_value(case: RetrievalCompareCase, field: str) -> str:
    value = case.raw.get(field)
    if value is None or value == "":
        return "__missing__"
    if isinstance(value, (list, tuple, set)):
        labels = [str(item).strip() for item in value if str(item).strip()]
        return "|".join(labels) if labels else "__missing__"
    return str(value).strip() or "__missing__"


def _summarize_grouped_results(
    group_labels: Sequence[dict[str, str]],
    relevant_counts: Sequence[int],
    dense_results: Sequence[_RetrieverCaseResult],
    bm25_results: Sequence[_RetrieverCaseResult],
    rrf_results: Sequence[_RetrieverCaseResult],
    dense_guarded_results: Sequence[_RetrieverCaseResult],
    oracle_union_results: Sequence[_RetrieverCaseResult],
    top_ks: Sequence[int],
) -> dict[str, Any]:
    groups: dict[str, Any] = {}
    retrievers = {
        "dense": dense_results,
        "bm25": bm25_results,
        "rrf": rrf_results,
        "dense_guarded": dense_guarded_results,
        "oracle_union": oracle_union_results,
    }
    for field in _GROUP_BY_FIELDS:
        values = sorted({labels.get(field, "__missing__") for labels in group_labels})
        field_summary: dict[str, Any] = {}
        for value in values:
            indexes = [idx for idx, labels in enumerate(group_labels) if labels.get(field, "__missing__") == value]
            if not indexes:
                continue
            block: dict[str, Any] = {"cases": len(indexes)}
            group_counts = [relevant_counts[idx] for idx in indexes]
            for name, results in retrievers.items():
                block[name] = _summarize_retriever([results[idx] for idx in indexes], group_counts, top_ks)
            field_summary[value] = block
        groups[field] = field_summary
    return groups


def _rows_seen_targets(rows: Sequence[dict[str, Any]], k: int) -> tuple[set[str], set[str]]:
    seen_files: set[str] = set()
    seen_symbols: set[str] = set()
    for row in rows[:k]:
        seen_files.update(str(x) for x in row.get("matched_expected_files", []) if str(x))
        seen_symbols.update(str(x) for x in row.get("matched_expected_symbols", []) if str(x))
    return seen_files, seen_symbols


def _hit_at(result: _RetrieverCaseResult, k: int) -> bool:
    return result.metrics.get(f"hit@{k}", result.metrics.get(f"recall@{k}", 0.0)) > 0


def _case_diagnostics(
    case: RetrievalCompareCase,
    dense: _RetrieverCaseResult,
    bm25: _RetrieverCaseResult,
    rrf: _RetrieverCaseResult,
    dense_guarded: _RetrieverCaseResult,
    oracle_union: _RetrieverCaseResult,
    max_k: int,
) -> dict[str, Any]:
    dense_hit = _hit_at(dense, max_k)
    bm25_hit = _hit_at(bm25, max_k)
    rrf_hit = _hit_at(rrf, max_k)
    dense_guarded_hit = _hit_at(dense_guarded, max_k)
    oracle_hit = _hit_at(oracle_union, max_k)
    seen_files, seen_symbols = _rows_seen_targets(oracle_union.retrieved, max_k)
    return {
        "dense_only_hit": bool(dense_hit and not bm25_hit),
        "bm25_only_hit": bool(bm25_hit and not dense_hit),
        "both_hit": bool(dense_hit and bm25_hit),
        "rrf_hit": bool(rrf_hit),
        "dense_guarded_hit": bool(dense_guarded_hit),
        "oracle_union_hit": bool(oracle_hit),
        "missing_gold_files": [path for path in case.gold_files if path not in seen_files],
        "missing_gold_symbols": [symbol for symbol in case.gold_symbols if symbol not in seen_symbols],
    }


def _table_markdown(summary: dict[str, Any], top_ks: Sequence[int]) -> str:
    retrievers = [
        ("dense", "Dense"),
        ("bm25", "BM25"),
        ("rrf", "RRF"),
        ("dense_guarded", "Dense guarded"),
        ("oracle_union", "Oracle union"),
    ]
    lines = [
        "| Metric | Dense | BM25 | RRF | Dense guarded | Oracle union |",
        "|---|---:|---:|---:|---:|---:|",
    ]

    def value(name: str, metric: str) -> float:
        block = summary.get(name) if isinstance(summary.get(name), dict) else {}
        return float(block.get(metric, 0.0))

    for k in top_ks:
        for label, metric, as_percent in (
            (f"Recall@{k} / Hit@{k}", f"recall@{k}", True),
            (f"MRR@{k}", f"mrr@{k}", False),
            (f"Precision@{k}", f"precision@{k}", True),
            (f"nDCG@{k}", f"ndcg@{k}", False),
            (f"FileRecall@{k}", f"file_recall@{k}", True),
            (f"SymbolRecall@{k}", f"symbol_recall@{k}", True),
            (f"ChunkRecall@{k}", f"chunk_recall@{k}", True),
            (f"TargetRecall@{k}", f"target_recall@{k}", True),
        ):
            cells = []
            for name, _title in retrievers:
                raw = value(name, metric)
                cells.append(f"{raw * 100:.2f}%" if as_percent else f"{raw:.6g}")
            lines.append(f"| {label} | " + " | ".join(cells) + " |")
    return "\n".join(lines)


def _index_info_json(store: SqliteStore) -> dict[str, Any]:
    try:
        return store.get_index_info()
    except Exception as exc:
        return {"error": type(exc).__name__, "message": str(exc)}


def sanitize_embedding_runtime_for_report(runtime: Mapping[str, Any]) -> dict[str, Any]:
    """Subset of embedding runtime safe for JSON reports (omit secrets such as api_key)."""
    keys = ("provider", "model", "dim", "api_base", "endpoint", "input_type", "device", "timeout_s")
    out: dict[str, Any] = {}
    for k in keys:
        if k not in runtime:
            continue
        val = runtime[k]
        if k == "dim":
            try:
                out[k] = int(val)
            except (TypeError, ValueError):
                out[k] = val
        elif k == "timeout_s":
            try:
                out[k] = int(val)
            except (TypeError, ValueError):
                out[k] = val
        else:
            out[k] = val
    return out


def _normalize_top_ks(top_ks: Sequence[int] | None) -> list[int]:
    values = [int(k) for k in (top_ks or [5, 10]) if int(k) > 0]
    if not values:
        values = [5, 10]
    return sorted(set(values))


def run_retrieval_compare_eval(
    *,
    store: SqliteStore,
    embedding_pipeline: DenseSearchPipeline,
    dataset_path: str,
    repo: str,
    commit: str,
    embedding_version: str,
    top_ks: Sequence[int] | None = None,
    include_commit_mismatches: bool = False,
    embedding_runtime: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    cases = load_retrieval_compare_cases(dataset_path)
    ks = _normalize_top_ks(top_ks)
    report_cases: list[dict[str, Any]] = []
    skipped_cases: list[dict[str, Any]] = []
    dense_results: list[_RetrieverCaseResult] = []
    bm25_results: list[_RetrieverCaseResult] = []
    rrf_results: list[_RetrieverCaseResult] = []
    dense_guarded_results: list[_RetrieverCaseResult] = []
    oracle_union_results: list[_RetrieverCaseResult] = []
    relevant_counts: list[int] = []
    group_labels: list[dict[str, str]] = []

    for case in cases:
        if (
            case.repo_sha
            and commit
            and case.repo_sha != commit
            and not include_commit_mismatches
        ):
            skipped_cases.append(
                {
                    "id": case.case_id,
                    "line": case.line,
                    "query": case.query,
                    "repo_sha": case.repo_sha,
                    "reason": "commit_mismatch",
                }
            )
            continue

        relevant = _relevant_chunks_for_case(store, repo, commit, case)
        dense = _run_dense(store, embedding_pipeline, case, relevant, embedding_version, ks)
        bm25 = _run_bm25(store, case, relevant, ks)
        rrf = _run_rrf_fusion(dense, bm25, case, relevant, ks)
        dense_guarded = _run_dense_guarded_fusion(dense, bm25, case, relevant, ks)
        oracle_union = _RetrieverCaseResult(
            metrics=_oracle_union_metrics(dense, bm25, case, relevant, ks),
            retrieved=[*dense.retrieved[: max(ks)], *bm25.retrieved[: max(ks)]],
            failure_reason="",
        )
        relevant_counts.append(len(relevant.chunk_ids))
        dense_results.append(dense)
        bm25_results.append(bm25)
        rrf_results.append(rrf)
        dense_guarded_results.append(dense_guarded)
        oracle_union_results.append(oracle_union)
        group_labels.append({field: _group_value(case, field) for field in _GROUP_BY_FIELDS})
        diagnostics = _case_diagnostics(case, dense, bm25, rrf, dense_guarded, oracle_union, max(ks))
        report_cases.append(
            {
                "id": case.case_id,
                "line": case.line,
                "query": case.query,
                "repo_sha": case.repo_sha,
                "gold_files": list(case.gold_files),
                "gold_symbols": list(case.gold_symbols),
                "gold_chunks": list(case.gold_chunks),
                "relevant_chunk_count": len(relevant.chunk_ids),
                "relevant_gold_counts": {
                    "files": len(case.gold_files),
                    "symbols": len(case.gold_symbols),
                    "direct_chunks": len(case.gold_chunks),
                    "relevant_chunks": len(relevant.chunk_ids),
                },
                **diagnostics,
                "dense": {
                    "metrics": dense.metrics,
                    "retrieved": dense.retrieved,
                    "failure_reason": dense.failure_reason,
                    **({"error": dense.error} if dense.error is not None else {}),
                },
                "bm25": {
                    "metrics": bm25.metrics,
                    "retrieved": bm25.retrieved,
                    "failure_reason": bm25.failure_reason,
                    **({"error": bm25.error} if bm25.error is not None else {}),
                },
                "rrf": {
                    "metrics": rrf.metrics,
                    "retrieved": rrf.retrieved,
                    "failure_reason": rrf.failure_reason,
                },
                "dense_guarded": {
                    "metrics": dense_guarded.metrics,
                    "retrieved": dense_guarded.retrieved,
                    "failure_reason": dense_guarded.failure_reason,
                },
                "oracle_union": {
                    "metrics": oracle_union.metrics,
                },
            }
        )

    summary: dict[str, Any] = {
        "loaded_cases": len(cases),
        "evaluated_cases": len(report_cases),
        "skipped_cases": len(skipped_cases),
        "skipped_commit_mismatch": sum(
            1 for case in skipped_cases if case.get("reason") == "commit_mismatch"
        ),
        "top_ks": ks,
        "embedding_version": embedding_version,
        "dense": _summarize_retriever(dense_results, relevant_counts, ks),
        "bm25": _summarize_retriever(bm25_results, relevant_counts, ks),
        "rrf": _summarize_retriever(rrf_results, relevant_counts, ks),
        "dense_guarded": _summarize_retriever(dense_guarded_results, relevant_counts, ks),
        "oracle_union": _summarize_retriever(oracle_union_results, relevant_counts, ks),
        "groups": _summarize_grouped_results(
            group_labels,
            relevant_counts,
            dense_results,
            bm25_results,
            rrf_results,
            dense_guarded_results,
            oracle_union_results,
            ks,
        ),
        "metric_notes": {
            "recall@k": "Backward-compatible alias for hit@k: at least one relevant chunk appears in top K.",
            "chunk_recall@k": "Unique relevant chunks retrieved in top K divided by all relevant chunks resolved from gold files/symbols/chunks.",
            "file_recall@k": "Gold file coverage in top K based on retrieved chunk paths.",
            "symbol_recall@k": "Gold symbol coverage in top K based on chunk primary symbols and symbol-derived relevant chunks.",
            "target_recall@k": "Average of non-empty file/symbol/direct-chunk recall families for the case.",
            "oracle_union": "Upper bound from dense top K union BM25 top K; not a deployable ranker.",
            "rrf": "Reciprocal-rank fusion of dense and BM25 result lists.",
            "dense_guarded": "Dense-first deployable fusion: keeps dense as the backbone and only allows conservative keyword inserts.",
        },
    }
    if embedding_runtime is not None:
        sanitized = sanitize_embedding_runtime_for_report(embedding_runtime)
        if sanitized:
            summary["embedding_runtime"] = sanitized
    return {
        "summary": summary,
        "table_markdown": _table_markdown(summary, ks),
        "cases": report_cases,
        "skipped_cases": skipped_cases,
        "index_info": _index_info_json(store),
        "repo": repo,
        "commit": commit,
        "dataset": str(Path(dataset_path).resolve()),
    }
