from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List

from .dsl import Query
from .retrieval import HybridRetrievalService


@dataclass
class EvalMetrics:
    recall_at_k: float
    mrr: float
    ndcg_at_10: float


def _dcg(relevances: List[int]) -> float:
    total = 0.0
    for idx, rel in enumerate(relevances, start=1):
        total += (2**rel - 1) / math.log2(idx + 1)
    return total


class Evaluator:
    def __init__(self, service: HybridRetrievalService) -> None:
        self.service = service

    def run(self, dataset_path: str, mode: str = "hybrid", top_k: int = 10) -> EvalMetrics:
        data = json.loads(Path(dataset_path).read_text(encoding="utf-8"))
        recalls: List[float] = []
        mrrs: List[float] = []
        ndcgs: List[float] = []

        for sample in data["samples"]:
            query_text = sample["query"]
            relevant = set(sample.get("relevant_ids", []))
            results = self.service.query(Query(text=query_text, mode=mode, top_k=top_k))
            result_ids = [r.result_id for r in results]

            hit_count = sum(1 for rid in result_ids if rid in relevant)
            recalls.append(hit_count / max(1, len(relevant)))

            rr = 0.0
            for idx, rid in enumerate(result_ids, start=1):
                if rid in relevant:
                    rr = 1.0 / idx
                    break
            mrrs.append(rr)

            gains = [1 if rid in relevant else 0 for rid in result_ids[:10]]
            ideal = sorted(gains, reverse=True)
            dcg = _dcg(gains)
            idcg = _dcg(ideal) if ideal else 0.0
            ndcgs.append(0.0 if idcg == 0 else dcg / idcg)

        return EvalMetrics(
            recall_at_k=sum(recalls) / max(1, len(recalls)),
            mrr=sum(mrrs) / max(1, len(mrrs)),
            ndcg_at_10=sum(ndcgs) / max(1, len(ndcgs)),
        )

    @staticmethod
    def format_metrics(metrics: EvalMetrics) -> Dict[str, float]:
        return {
            "recall@k": round(metrics.recall_at_k, 4),
            "mrr": round(metrics.mrr, 4),
            "ndcg@10": round(metrics.ndcg_at_10, 4),
        }
