"""基于 :func:`entity_query.find_entity` 的离线评测（与混合检索 ``eval`` 独立）。"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Set

from .entity_query import EntityHit, find_entity
from .storage import SqliteStore


@dataclass
class EntityEvalMetrics:
    recall_at_k: float
    mrr: float
    ndcg_at_10: float


@dataclass
class EntityEvalReport:
    """聚合指标 + 每条 query 的返回明细。"""

    metrics: EntityEvalMetrics
    queries: List[Dict[str, Any]]


def _dcg(relevances: List[int]) -> float:
    total = 0.0
    for idx, rel in enumerate(relevances, start=1):
        total += (2**rel - 1) / math.log2(idx + 1)
    return total


def run_entity_eval(
    store: SqliteStore,
    dataset_path: str,
    top_k: int = 10,
) -> EntityEvalReport:
    """读取数据集 JSON，对每条样本调用 ``find_entity``，计算与 ``eval`` 相同的 recall@k / MRR / nDCG@10，并返回每条 query 的命中列表。

    数据集格式::

        {
          "samples": [
            {
              "entity_query": {
                "type": "class",
                "name": "AbstractByteBuf",
                "match": "exact",
                "package_contains": ""
              },
              "relevant_ids": ["semanticdb maven ..."]
            }
          ]
        }

    ``entity_query`` 字段与 :func:`find_entity` 一致；``package_contains`` 可省略。
    """
    data: Dict[str, Any] = json.loads(Path(dataset_path).read_text(encoding="utf-8"))
    samples: List[Dict[str, Any]] = data["samples"]
    recalls: List[float] = []
    mrrs: List[float] = []
    ndcgs: List[float] = []
    query_rows: List[Dict[str, Any]] = []

    for idx, sample in enumerate(samples):
        eq = sample["entity_query"]
        relevant: Set[str] = set(sample.get("relevant_ids", []))
        t = eq["type"]
        name = eq["name"]
        match = eq.get("match", "contains")
        pkg = eq.get("package_contains", "") or ""

        hits: List[EntityHit] = find_entity(
            store,
            type=t,
            name=name,
            match=match,
            package_contains=pkg,
            limit=top_k,
        )
        result_ids = [h.symbol_id for h in hits[:top_k]]

        hit_count = sum(1 for rid in result_ids if rid in relevant)
        rec = hit_count / max(1, len(relevant))
        recalls.append(rec)

        rr = 0.0
        for rank, rid in enumerate(result_ids, start=1):
            if rid in relevant:
                rr = 1.0 / rank
                break
        mrrs.append(rr)

        gains = [1 if rid in relevant else 0 for rid in result_ids[:10]]
        ideal = sorted(gains, reverse=True)
        dcg = _dcg(gains)
        idcg = _dcg(ideal) if ideal else 0.0
        ndcg = 0.0 if idcg == 0 else dcg / idcg
        ndcgs.append(ndcg)

        results_detail: List[Dict[str, Any]] = []
        for rank, h in enumerate(hits[:top_k], start=1):
            results_detail.append(
                {
                    "rank": rank,
                    "symbol_id": h.symbol_id,
                    "display_name": h.display_name,
                    "kind": h.kind,
                    "package": h.package,
                    "is_relevant": h.symbol_id in relevant,
                }
            )

        query_rows.append(
            {
                "index": idx,
                "entity_query": dict(eq),
                "relevant_ids": list(sample.get("relevant_ids", [])),
                "returned_count": len(result_ids),
                "hits_in_relevant": hit_count,
                "recall@k": rec,
                "mrr": rr,
                "ndcg@10": ndcg,
                "results": results_detail,
            }
        )

    n = max(1, len(samples))
    metrics = EntityEvalMetrics(
        recall_at_k=sum(recalls) / n,
        mrr=sum(mrrs) / n,
        ndcg_at_10=sum(ndcgs) / n,
    )
    return EntityEvalReport(metrics=metrics, queries=query_rows)


def format_entity_eval_metrics(m: EntityEvalMetrics) -> Dict[str, float]:
    return {
        "recall@k": round(m.recall_at_k, 4),
        "mrr": round(m.mrr, 4),
        "ndcg@10": round(m.ndcg_at_10, 4),
    }


def entity_eval_report_to_json(report: EntityEvalReport) -> Dict[str, Any]:
    """供 CLI 打印的完整结构（含每条 query）。"""
    return {
        "summary": format_entity_eval_metrics(report.metrics),
        "queries": report.queries,
    }
