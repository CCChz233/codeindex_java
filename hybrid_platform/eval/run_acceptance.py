from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
import sys

BASE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE_DIR))

from hybrid_platform.dsl import Query
from hybrid_platform.embedding import DeterministicEmbedder, EmbeddingPipeline
from hybrid_platform.observability import MetricsRecorder
from hybrid_platform.retrieval import HybridRetrievalService
from hybrid_platform.storage import SqliteStore
from hybrid_platform.vector_store import SqliteVectorStore
from hybrid_platform.vector_store_lancedb import LanceDbVectorStore


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Acceptance benchmark for retrieval stack")
    parser.add_argument("--db", default=str(BASE_DIR / "examples" / "demo.db"))
    parser.add_argument("--queries", default=str(BASE_DIR / "examples" / "queries_20.json"))
    parser.add_argument("--mode", choices=["structure", "semantic", "hybrid"], default="hybrid")
    parser.add_argument("--top-k", type=int, default=10)
    parser.add_argument("--embedding-version", default="v1")
    parser.add_argument("--vector-backend", choices=["sqlite", "lancedb"], default="sqlite")
    parser.add_argument("--lancedb-uri", default="")
    parser.add_argument("--lancedb-table", default="chunk_vectors")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    queries = json.loads(Path(args.queries).read_text(encoding="utf-8"))["queries"]
    store = SqliteStore(str(args.db))
    if args.vector_backend == "lancedb":
        if not args.lancedb_uri:
            raise ValueError("--vector-backend=lancedb 时必须提供 --lancedb-uri")
        vector_store = LanceDbVectorStore(uri=args.lancedb_uri, table=args.lancedb_table)
    else:
        vector_store = SqliteVectorStore(store)
    pipeline = EmbeddingPipeline(
        store,
        embedder=DeterministicEmbedder(),
        vector_search_store=vector_store,
        vector_write_stores=[vector_store],
    )
    service = HybridRetrievalService(
        store,
        embedding_pipeline=pipeline,
        default_embedding_version=args.embedding_version,
    )
    metrics = MetricsRecorder()
    with metrics.timer("acceptance_total_ms"):
        for q in queries:
            with metrics.timer("query_ms"):
                results = service.query(
                    Query(text=q, mode=args.mode, top_k=args.top_k),
                    embedding_version=args.embedding_version,
                )
                if results:
                    metrics.inc("queries_with_hits")
                metrics.inc("queries_total")
    store.close()

    snapshot = metrics.snapshot()
    snapshot["config"] = {
        "db": str(args.db),
        "queries": str(args.queries),
        "mode": args.mode,
        "top_k": int(args.top_k),
        "embedding_version": args.embedding_version,
        "vector_backend": args.vector_backend,
        "lancedb_uri": args.lancedb_uri,
        "lancedb_table": args.lancedb_table,
    }
    print(json.dumps(snapshot, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    start = time.time()
    main()
    print(f"elapsed_s={time.time()-start:.3f}")
