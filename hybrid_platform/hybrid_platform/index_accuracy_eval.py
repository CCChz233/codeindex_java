from __future__ import annotations

import json
import math
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Sequence

from .dsl import Query, callees_of, callers_of, def_of, refs_of
from .entity_query import EntityHit, find_entity
from .index_contract import UnsupportedCapabilityError
from .models import QueryResult
from .retrieval import HybridRetrievalService
from .storage import SqliteStore

AccuracyKind = Literal["entity", "retrieval", "graph"]
ExpectedUnitKind = Literal["chunks", "symbols", "files", "none"]

SUPPORTED_KINDS = {"entity", "retrieval", "graph"}
GRAPH_OPS = {
    "def_of": def_of,
    "def-of": def_of,
    "refs_of": refs_of,
    "refs-of": refs_of,
    "callers_of": callers_of,
    "callers-of": callers_of,
    "callees_of": callees_of,
    "callees-of": callees_of,
}


@dataclass(frozen=True)
class AccuracyCase:
    case_id: str
    kind: AccuracyKind
    raw: dict[str, Any]
    line: int | None = None


@dataclass(frozen=True)
class ExpectedUnits:
    kind: ExpectedUnitKind
    units: frozenset[str]


@dataclass
class _CaseMetric:
    success: float
    recall: float
    mrr: float
    ndcg: float
    unsupported: bool = False
    empty_expected: bool = False


def _dcg(relevances: Sequence[int]) -> float:
    total = 0.0
    for idx, rel in enumerate(relevances, start=1):
        total += (2**rel - 1) / math.log2(idx + 1)
    return total


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


def _expected_units(expected: dict[str, Any]) -> ExpectedUnits:
    chunks = _as_str_list(expected.get("chunks") or expected.get("chunk_ids"))
    if chunks:
        return ExpectedUnits("chunks", frozenset(chunks))
    symbols = _as_str_list(expected.get("symbols") or expected.get("symbol_ids"))
    if symbols:
        return ExpectedUnits("symbols", frozenset(symbols))
    files = _as_str_list(expected.get("files") or expected.get("paths"))
    if files:
        return ExpectedUnits("files", frozenset(_normalize_path(x) for x in files))
    return ExpectedUnits("none", frozenset())


def load_accuracy_cases(dataset_path: str) -> list[AccuracyCase]:
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
        for idx, row in enumerate(samples):
            if not isinstance(row, dict):
                raise ValueError(f"dataset sample {idx} must be a JSON object")
            rows.append((row, None))

    cases: list[AccuracyCase] = []
    for idx, (row, line_no) in enumerate(rows):
        row = _normalize_case_row(row)
        kind = str(row.get("kind", "")).strip().lower()
        if kind not in SUPPORTED_KINDS:
            loc = f"line {line_no}" if line_no is not None else f"sample {idx}"
            raise ValueError(f"{loc}: unsupported kind {kind!r}; expected entity, retrieval, or graph")
        case_id = str(row.get("id") or (f"line-{line_no}" if line_no is not None else f"case-{idx}"))
        cases.append(AccuracyCase(case_id=case_id, kind=kind, raw=row, line=line_no))  # type: ignore[arg-type]
    return cases


def _normalize_case_row(row: dict[str, Any]) -> dict[str, Any]:
    """Accept the native eval-index format and the reviewed Spring flat JSONL format.

    The reviewed Spring rows have `sample_id`, `query`, `gold_files`, and `gold_symbols`
    at top level. They are retrieval cases; file relevance is the default because it is
    stable across SCIP/SemanticDB/tree-sitter symbol id formats.
    """
    if str(row.get("kind", "")).strip():
        return row
    query = str(row.get("query", "")).strip()
    gold_files = _as_str_list(row.get("gold_files"))
    gold_symbols = _as_str_list(row.get("gold_symbols"))
    if not query or (not gold_files and not gold_symbols):
        return row
    expected: dict[str, Any] = {"files": gold_files} if gold_files else {"symbols": gold_symbols}
    return {
        **row,
        "id": row.get("id") or row.get("sample_id") or row.get("case_id"),
        "kind": "retrieval",
        "query": query,
        "expected": expected,
        "source_format": "spring_reviewed_flat",
    }


def _index_info_json(store: SqliteStore) -> dict[str, Any]:
    try:
        return store.get_index_info()
    except Exception as exc:
        return {"error": type(exc).__name__, "message": str(exc)}


def _result_path(store: SqliteStore, result: QueryResult | dict[str, Any]) -> str:
    if isinstance(result, QueryResult):
        payload = result.payload or {}
        result_id = result.result_id
        result_type = result.result_type
    else:
        payload = result.get("payload") or {}
        result_id = str(result.get("id") or result.get("result_id") or "")
        result_type = str(result.get("type") or result.get("result_type") or "")
    path = payload.get("path") if isinstance(payload, dict) else None
    if isinstance(path, str) and path.strip():
        return path.strip()
    if result_type == "chunk":
        meta = store.fetch_chunk_metadata(result_id, include_content=False)
        return str((meta or {}).get("path") or "")
    if result_type == "symbol":
        return store.fetch_relative_path_for_symbol(result_id) or ""
    return ""


def _result_symbols(store: SqliteStore, result: QueryResult | dict[str, Any]) -> set[str]:
    if isinstance(result, QueryResult):
        payload = result.payload or {}
        result_id = result.result_id
        result_type = result.result_type
    else:
        payload = result.get("payload") or {}
        result_id = str(result.get("id") or result.get("result_id") or "")
        result_type = str(result.get("type") or result.get("result_type") or "")
    out: set[str] = set()
    if result_type == "symbol" and result_id:
        out.add(result_id)
    if isinstance(payload, dict):
        sid = payload.get("symbol_id")
        if isinstance(sid, str) and sid.strip():
            out.add(sid.strip())
    if result_type == "chunk" and result_id:
        out.update(store.fetch_chunk_primary_symbols(result_id))
    return out


def _matched_units(
    store: SqliteStore,
    result: QueryResult | dict[str, Any],
    expected: ExpectedUnits,
) -> set[str]:
    if expected.kind == "none":
        return set()
    if isinstance(result, QueryResult):
        result_id = result.result_id
        result_type = result.result_type
    else:
        result_id = str(result.get("id") or result.get("result_id") or "")
        result_type = str(result.get("type") or result.get("result_type") or "")

    if expected.kind == "chunks":
        return {result_id} if result_type == "chunk" and result_id in expected.units else set()
    if expected.kind == "symbols":
        matches = _result_symbols(store, result) & set(expected.units)
        if matches:
            return matches
        if result_type == "chunk":
            # Defensive fallback for chunks whose primary_symbol_ids are missing but whose id encodes the symbol.
            return {sid for sid in expected.units if sid and sid in result_id}
        return set()
    path = _result_path(store, result)
    return {f for f in expected.units if _path_matches(path, f)}


def _metric_from_ranked_matches(
    ranked_matches: list[set[str]],
    expected_count: int,
    top_k: int,
) -> _CaseMetric:
    if expected_count <= 0:
        return _CaseMetric(0.0, 0.0, 0.0, 0.0, empty_expected=True)
    matched: set[str] = set()
    rr = 0.0
    gains: list[int] = []
    for rank, units in enumerate(ranked_matches[:top_k], start=1):
        new_units = units - matched
        is_hit = bool(new_units)
        gains.append(1 if is_hit else 0)
        if units and rr == 0.0:
            rr = 1.0 / rank
        matched.update(new_units)
    ideal_hits = min(expected_count, top_k)
    ideal = [1] * ideal_hits + [0] * max(0, min(top_k, len(gains)) - ideal_hits)
    dcg = _dcg(gains[:10])
    idcg = _dcg(ideal[:10])
    recall = len(matched) / max(1, expected_count)
    return _CaseMetric(
        success=1.0 if matched else 0.0,
        recall=recall,
        mrr=rr,
        ndcg=0.0 if idcg == 0 else dcg / idcg,
    )


def _query_results_to_rows(
    store: SqliteStore,
    results: Sequence[QueryResult],
    expected: ExpectedUnits,
) -> tuple[list[dict[str, Any]], list[set[str]]]:
    rows: list[dict[str, Any]] = []
    ranked_matches: list[set[str]] = []
    for rank, r in enumerate(results, start=1):
        matches = _matched_units(store, r, expected)
        ranked_matches.append(matches)
        rows.append(
            {
                "rank": rank,
                "id": r.result_id,
                "type": r.result_type,
                "score": float(r.score),
                "explain": r.explain,
                "payload": r.payload,
                "is_relevant": bool(matches),
                "matched_expected_units": sorted(matches),
            }
        )
    return rows, ranked_matches


def _entity_hits_to_rows(
    store: SqliteStore,
    hits: Sequence[EntityHit],
    expected: ExpectedUnits,
) -> tuple[list[dict[str, Any]], list[set[str]]]:
    rows: list[dict[str, Any]] = []
    ranked_matches: list[set[str]] = []
    for rank, h in enumerate(hits, start=1):
        result = {
            "id": h.symbol_id,
            "type": "symbol",
            "payload": {
                "symbol_id": h.symbol_id,
                "path": store.fetch_relative_path_for_symbol(h.symbol_id) or "",
            },
        }
        matches = _matched_units(store, result, expected)
        ranked_matches.append(matches)
        rows.append(
            {
                "rank": rank,
                "id": h.symbol_id,
                "type": "symbol",
                "display_name": h.display_name,
                "kind": h.kind,
                "package": h.package,
                "language": h.language,
                "enclosing_symbol": h.enclosing_symbol,
                "is_relevant": bool(matches),
                "matched_expected_units": sorted(matches),
            }
        )
    return rows, ranked_matches


def _failure_reason(metric: _CaseMetric, returned_count: int) -> str:
    if metric.unsupported:
        return "unsupported_capability"
    if metric.empty_expected:
        return "empty_expected"
    if returned_count == 0:
        return "no_results"
    if metric.success <= 0:
        return "no_relevant_hit"
    return ""


def _run_entity_case(
    store: SqliteStore,
    case: AccuracyCase,
    expected: ExpectedUnits,
    top_k: int,
) -> tuple[dict[str, Any], _CaseMetric]:
    eq = case.raw.get("entity_query")
    if not isinstance(eq, dict):
        raise ValueError(f"{case.case_id}: entity case requires entity_query object")
    hits = find_entity(
        store,
        type=str(eq.get("type", "any")),
        name=str(eq.get("name", "")),
        match=str(eq.get("match", "contains")),  # type: ignore[arg-type]
        package_contains=str(eq.get("package_contains", "") or ""),
        limit=top_k,
    )
    rows, ranked_matches = _entity_hits_to_rows(store, hits[:top_k], expected)
    metric = _metric_from_ranked_matches(ranked_matches, len(expected.units), top_k)
    return (
        {
            "entity_query": dict(eq),
            "retrieved": rows,
        },
        metric,
    )


def _run_retrieval_case(
    store: SqliteStore,
    service: HybridRetrievalService,
    case: AccuracyCase,
    expected: ExpectedUnits,
    top_k: int,
    default_mode: str,
    embedding_version: str | None,
) -> tuple[dict[str, Any], _CaseMetric]:
    query = str(case.raw.get("query", "")).strip()
    if not query:
        raise ValueError(f"{case.case_id}: retrieval case requires non-empty query")
    mode = str(case.raw.get("mode") or default_mode or "hybrid")
    blend_strategy = str(case.raw.get("blend_strategy") or "linear")
    results = service.query(
        Query(text=query, mode=mode, top_k=top_k, blend_strategy=blend_strategy),
        embedding_version=embedding_version,
        include_code=False,
    )
    rows, ranked_matches = _query_results_to_rows(store, results[:top_k], expected)
    metric = _metric_from_ranked_matches(ranked_matches, len(expected.units), top_k)
    return (
        {
            "query": query,
            "mode": mode,
            "blend_strategy": blend_strategy,
            "retrieved": rows,
        },
        metric,
    )


def _run_graph_case(
    store: SqliteStore,
    service: HybridRetrievalService,
    case: AccuracyCase,
    expected: ExpectedUnits,
    top_k: int,
) -> tuple[dict[str, Any], _CaseMetric]:
    op = str(case.raw.get("op", "")).strip()
    symbol_id = str(case.raw.get("symbol_id", "")).strip()
    if op not in GRAPH_OPS:
        raise ValueError(f"{case.case_id}: unsupported graph op {op!r}")
    if not symbol_id:
        raise ValueError(f"{case.case_id}: graph case requires symbol_id")
    results = service.query(GRAPH_OPS[op](symbol_id, top_k=top_k), include_code=False)
    rows, ranked_matches = _query_results_to_rows(store, results[:top_k], expected)
    metric = _metric_from_ranked_matches(ranked_matches, len(expected.units), top_k)
    return (
        {
            "op": op,
            "symbol_id": symbol_id,
            "retrieved": rows,
        },
        metric,
    )


def _empty_metrics() -> dict[str, Any]:
    return {
        "cases": 0,
        "success@k": 0.0,
        "recall@k": 0.0,
        "mrr": 0.0,
        "ndcg@10": 0.0,
        "unsupported_count": 0,
        "empty_expected_count": 0,
    }


def _summarize(metrics: Sequence[_CaseMetric]) -> dict[str, Any]:
    if not metrics:
        return _empty_metrics()
    n = len(metrics)
    return {
        "cases": n,
        "success@k": round(sum(m.success for m in metrics) / n, 4),
        "recall@k": round(sum(m.recall for m in metrics) / n, 4),
        "mrr": round(sum(m.mrr for m in metrics) / n, 4),
        "ndcg@10": round(sum(m.ndcg for m in metrics) / n, 4),
        "unsupported_count": sum(1 for m in metrics if m.unsupported),
        "empty_expected_count": sum(1 for m in metrics if m.empty_expected),
    }


def run_index_accuracy_eval(
    *,
    store: SqliteStore,
    service: HybridRetrievalService,
    dataset_path: str,
    repo: str,
    commit: str,
    top_k: int = 10,
    mode: str = "hybrid",
    embedding_version: str | None = None,
) -> dict[str, Any]:
    cases = load_accuracy_cases(dataset_path)
    k = max(1, int(top_k))
    default_mode = (mode or "hybrid").strip().lower()
    report_cases: list[dict[str, Any]] = []
    metrics: list[_CaseMetric] = []
    by_kind_metrics: dict[str, list[_CaseMetric]] = {kind: [] for kind in sorted(SUPPORTED_KINDS)}
    kind_counts: dict[str, int] = {kind: 0 for kind in sorted(SUPPORTED_KINDS)}

    for case in cases:
        kind_counts[case.kind] += 1
        expected_raw = case.raw.get("expected") or {}
        if not isinstance(expected_raw, dict):
            raise ValueError(f"{case.case_id}: expected must be an object")
        expected = _expected_units(expected_raw)
        base: dict[str, Any] = {
            "id": case.case_id,
            "kind": case.kind,
            "line": case.line,
            "expected": dict(expected_raw),
            "expected_unit_kind": expected.kind,
            "expected_units": sorted(expected.units),
        }
        forced_failure_reason = ""
        try:
            if case.kind == "entity":
                detail, metric = _run_entity_case(store, case, expected, k)
            elif case.kind == "retrieval":
                detail, metric = _run_retrieval_case(
                    store,
                    service,
                    case,
                    expected,
                    k,
                    default_mode,
                    embedding_version,
                )
            else:
                detail, metric = _run_graph_case(store, service, case, expected, k)
        except UnsupportedCapabilityError as exc:
            detail = {
                "retrieved": [],
                "error": {
                    "type": type(exc).__name__,
                    "message": str(exc),
                    "capability": exc.capability,
                    "source_mode": exc.source_mode,
                },
            }
            metric = _CaseMetric(0.0, 0.0, 0.0, 0.0, unsupported=True)
        except Exception as exc:
            forced_failure_reason = "case_error"
            detail = {
                "retrieved": [],
                "error": {
                    "type": type(exc).__name__,
                    "message": str(exc),
                },
            }
            metric = _CaseMetric(0.0, 0.0, 0.0, 0.0)

        retrieved = detail.get("retrieved")
        returned_count = len(retrieved) if isinstance(retrieved, list) else 0
        failure_reason = forced_failure_reason or _failure_reason(metric, returned_count)
        case_row = {
            **base,
            **detail,
            "metrics": {
                "success@k": metric.success,
                "recall@k": metric.recall,
                "mrr": metric.mrr,
                "ndcg@10": metric.ndcg,
            },
            "is_relevant": metric.success > 0,
            "failure_reason": failure_reason,
        }
        report_cases.append(case_row)
        metrics.append(metric)
        by_kind_metrics[case.kind].append(metric)

    summary = _summarize(metrics)
    summary.update(
        {
            "samples": len(cases),
            "kind_counts": kind_counts,
            "top_k": k,
            "mode": default_mode,
        }
    )
    return {
        "summary": summary,
        "by_kind": {kind: _summarize(items) for kind, items in sorted(by_kind_metrics.items())},
        "cases": report_cases,
        "index_info": _index_info_json(store),
        "repo": repo,
        "commit": commit,
        "dataset": str(Path(dataset_path).resolve()),
    }
