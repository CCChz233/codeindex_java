from __future__ import annotations

import json
from collections.abc import Iterable
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


def _metrics_for_results(
    result_ids: Sequence[str],
    relevant_ids: set[str],
    top_ks: Sequence[int],
) -> dict[str, float]:
    metrics: dict[str, float] = {}
    relevant_count = len(relevant_ids)
    for k in top_ks:
        top_ids = list(result_ids[:k])
        seen_hits: set[str] = set()
        first_rank = 0
        for rank, chunk_id in enumerate(top_ids, start=1):
            if chunk_id not in relevant_ids:
                continue
            seen_hits.add(chunk_id)
            if first_rank == 0:
                first_rank = rank
        metrics[f"recall@{k}"] = 1.0 if seen_hits else 0.0
        metrics[f"mrr@{k}"] = (1.0 / first_rank) if first_rank else 0.0
        metrics[f"chunk_recall@{k}"] = (
            len(seen_hits) / relevant_count if relevant_count > 0 else 0.0
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
    if metrics.get(f"recall@{max_k}", 0.0) <= 0:
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
    result_ids = [chunk_id for chunk_id, _ in hits]
    metrics = _metrics_for_results(result_ids, set(relevant.chunk_ids), top_ks)
    retrieved = [
        _chunk_meta_row(store, chunk_id, score, rank, relevant, case)
        for rank, (chunk_id, score) in enumerate(hits, start=1)
    ]
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
    result_ids = [result.result_id for result in results]
    metrics = _metrics_for_results(result_ids, set(relevant.chunk_ids), top_ks)
    retrieved = [
        _chunk_meta_row(store, result.result_id, result.score, rank, relevant, case)
        for rank, result in enumerate(results, start=1)
    ]
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
    for k in top_ks:
        if n == 0:
            out[f"recall@{k}"] = 0.0
            out[f"mrr@{k}"] = 0.0
            out[f"chunk_recall@{k}"] = 0.0
            continue
        out[f"recall@{k}"] = round(
            sum(result.metrics.get(f"recall@{k}", 0.0) for result in case_results) / n,
            6,
        )
        out[f"mrr@{k}"] = round(
            sum(result.metrics.get(f"mrr@{k}", 0.0) for result in case_results) / n,
            6,
        )
        out[f"chunk_recall@{k}"] = round(
            sum(result.metrics.get(f"chunk_recall@{k}", 0.0) for result in case_results) / n,
            6,
        )
    return out


def _table_markdown(summary: dict[str, Any], top_ks: Sequence[int]) -> str:
    lines = [
        "| Metric | Dense | BM25 |",
        "|---|---:|---:|",
    ]
    for k in top_ks:
        dense_recall = float(summary["dense"].get(f"recall@{k}", 0.0))
        bm25_recall = float(summary["bm25"].get(f"recall@{k}", 0.0))
        lines.append(f"| Recall@{k} | {dense_recall * 100:.2f}% | {bm25_recall * 100:.2f}% |")
        lines.append(
            f"| MRR@{k} | {float(summary['dense'].get(f'mrr@{k}', 0.0)):.6g} | "
            f"{float(summary['bm25'].get(f'mrr@{k}', 0.0)):.6g} |"
        )
    return "\n".join(lines)


def _index_info_json(store: SqliteStore) -> dict[str, Any]:
    try:
        return store.get_index_info()
    except Exception as exc:
        return {"error": type(exc).__name__, "message": str(exc)}


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
) -> dict[str, Any]:
    cases = load_retrieval_compare_cases(dataset_path)
    ks = _normalize_top_ks(top_ks)
    report_cases: list[dict[str, Any]] = []
    skipped_cases: list[dict[str, Any]] = []
    dense_results: list[_RetrieverCaseResult] = []
    bm25_results: list[_RetrieverCaseResult] = []
    relevant_counts: list[int] = []

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
        relevant_counts.append(len(relevant.chunk_ids))
        dense_results.append(dense)
        bm25_results.append(bm25)
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
    }
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
