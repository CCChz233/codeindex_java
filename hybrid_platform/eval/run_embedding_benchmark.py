from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE_DIR))

from hybrid_platform.config import AppConfig
from hybrid_platform.dsl import Query
from hybrid_platform.observability import MetricsRecorder
from hybrid_platform.retrieval import HybridRetrievalService
from hybrid_platform.service_api import _make_embedding_pipeline
from hybrid_platform.storage import SqliteStore


def _dcg(relevances: list[int]) -> float:
    total = 0.0
    for idx, rel in enumerate(relevances, start=1):
        total += (2**rel - 1) / math.log2(idx + 1)
    return total


def _evaluate_backend(
    *,
    db_path: str,
    dataset_path: str,
    mode: str,
    top_k: int,
    embedding_version: str,
    embedding_runtime: dict[str, object],
    vector_runtime: dict[str, object],
) -> dict[str, object]:
    data = json.loads(Path(dataset_path).read_text(encoding="utf-8"))
    store = SqliteStore(db_path)
    try:
        pipeline = _make_embedding_pipeline(store, embedding_runtime=embedding_runtime, vector_runtime=vector_runtime)
        service = HybridRetrievalService(
            store,
            embedding_pipeline=pipeline,
            default_embedding_version=embedding_version,
        )
        metrics = MetricsRecorder()
        recalls: list[float] = []
        mrrs: list[float] = []
        ndcgs: list[float] = []
        with metrics.timer("benchmark_total_ms"):
            for sample in data["samples"]:
                with metrics.timer("query_ms"):
                    results = service.query(
                        Query(text=sample["query"], mode=mode, top_k=top_k),
                        embedding_version=embedding_version,
                    )
                metrics.inc("queries_total")
                if results:
                    metrics.inc("queries_with_hits")
                relevant = set(sample.get("relevant_ids", []))
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
        snapshot = metrics.snapshot()
        snapshot["metrics"] = {
            "recall@k": round(sum(recalls) / max(1, len(recalls)), 4),
            "mrr": round(sum(mrrs) / max(1, len(mrrs)), 4),
            "ndcg@10": round(sum(ndcgs) / max(1, len(ndcgs)), 4),
        }
        return snapshot
    finally:
        store.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Embedding/vector backend benchmark")
    parser.add_argument("--config", default=str(BASE_DIR / "config" / "default_config.json"))
    parser.add_argument("--db", default=str(BASE_DIR / "examples" / "demo.db"))
    parser.add_argument("--dataset", default=str(BASE_DIR / "examples" / "eval_dataset.json"))
    parser.add_argument("--mode", choices=["semantic", "hybrid"], default="semantic")
    parser.add_argument("--top-k", type=int, default=10)
    parser.add_argument("--embedding-version", default=None)
    parser.add_argument("--vector-backends", default="sqlite", help="comma-separated: sqlite,lancedb")
    parser.add_argument("--lancedb-uri", default="")
    parser.add_argument("--lancedb-table", default="chunk_vectors")
    parser.add_argument("--lancedb-metric", default="cosine")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    cfg = AppConfig.load(args.config)
    embedding_runtime = {
        "provider": str(cfg.get("embedding", "provider", "deterministic")).strip().lower(),
        "model": str(cfg.get("embedding", "model", "deterministic-hash-v1")).strip(),
        "dim": int(cfg.get("embedding", "dim", 128)),
        "api_base": str(cfg.get("embedding", "api_base", "")).strip(),
        "api_key": str(cfg.get("embedding", "api_key", "")).strip(),
        "timeout_s": int(cfg.get("embedding", "timeout_s", 30)),
        "endpoint": str(cfg.get("embedding", "endpoint", "/embeddings")).strip(),
        "batch_size": int(cfg.get("embedding", "batch_size", 64)),
        "max_workers": int(cfg.get("embedding", "max_workers", 4)),
        "max_retries": int(cfg.get("embedding", "max_retries", 2)),
        "retry_backoff_s": float(cfg.get("embedding", "retry_backoff_s", 0.5)),
        "input_type": str(cfg.get("embedding", "input_type", "document")).strip(),
        "device": str(cfg.get("embedding", "device", "cpu")).strip(),
        "llama": cfg.get("embedding", "llama", {}) or {},
    }
    embedding_version = args.embedding_version or str(cfg.get("embedding", "version", "v1"))
    backends = [b.strip().lower() for b in str(args.vector_backends).split(",") if b.strip()]
    report: dict[str, object] = {
        "config": {
            "db": args.db,
            "dataset": args.dataset,
            "mode": args.mode,
            "top_k": args.top_k,
            "embedding_version": embedding_version,
            "vector_backends": backends,
        },
        "backends": {},
    }
    for backend in backends:
        vector_runtime = {
            "backend": backend,
            "write_mode": "sqlite_only",
            "lancedb": {
                "uri": args.lancedb_uri,
                "table": args.lancedb_table,
                "metric": args.lancedb_metric,
            },
        }
        report["backends"][backend] = _evaluate_backend(
            db_path=args.db,
            dataset_path=args.dataset,
            mode=args.mode,
            top_k=args.top_k,
            embedding_version=embedding_version,
            embedding_runtime=embedding_runtime,
            vector_runtime=vector_runtime,
        )
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
