"""将 Java 风格 golden（FQCN / #method）映射到 chunk_id，评测纯 semantic 检索。"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from typing import Any, Dict, List, Set

from .storage import SqliteStore


def _dcg(relevances: List[int]) -> float:
    total = 0.0
    for idx, rel in enumerate(relevances, start=1):
        total += (2**rel - 1) / math.log2(idx + 1)
    return total


def _resolve_type_fqcn(gold: str) -> str:
    g = gold.strip().replace("`", "")
    return g.split("#", 1)[0]


def _method_simple_hint(gold: str) -> str | None:
    if "#" not in gold:
        return None
    _, rest = gold.split("#", 1)
    rest = rest.strip().replace("`", "")
    if not rest:
        return None
    # createRequest/2 -> createRequest; getInterceptor/0 -> getInterceptor
    name = rest.split("/")[0]
    if "(" in name:
        name = name.split("(")[0]
    return name if name else None


def _document_ids_for_fqcn(conn: Any, repo: str, commit: str, fqcn: str) -> List[str]:
    """自顶向下缩短 FQCN，直到在 main/java 或 main/kotlin 下找到对应源文件。"""
    parts = fqcn.split(".")
    for k in range(len(parts), 0, -1):
        prefix = "/".join(parts[:k])
        for ext, sub in (("java", "java"), ("kt", "kotlin")):
            suffix = f"{prefix}.{ext}"
            pattern = f"%/main/{sub}/{suffix}"
            cur = conn.execute(
                """
                SELECT document_id FROM documents
                WHERE repo = ? AND commit_hash = ? AND relative_path LIKE ?
                """,
                (repo, commit, pattern),
            )
            rows = [str(r[0]) for r in cur.fetchall()]
            if rows:
                return rows
    return []


def _chunks_for_documents(conn: Any, document_ids: List[str]) -> List[tuple[str, str]]:
    out: List[tuple[str, str]] = []
    for doc_id in document_ids:
        cur = conn.execute(
            "SELECT chunk_id, COALESCE(primary_symbol_ids, '') FROM chunks WHERE document_id = ?",
            (doc_id,),
        )
        out.extend((str(r[0]), str(r[1])) for r in cur.fetchall())
    return out


def golden_to_relevant_chunk_ids(
    store: SqliteStore,
    repo: str,
    commit: str,
    gold: str,
) -> Set[str]:
    fqcn = _resolve_type_fqcn(gold)
    doc_ids = _document_ids_for_fqcn(store.conn, repo, commit, fqcn)
    if not doc_ids:
        return set()
    method_hint = _method_simple_hint(gold)
    rows = _chunks_for_documents(store.conn, doc_ids)
    if not method_hint:
        return {cid for cid, _ in rows}
    matched: Set[str] = set()
    for cid, psym in rows:
        if method_hint in psym or method_hint in cid:
            matched.add(cid)
    if matched:
        return matched
    return {cid for cid, _ in rows}


def sample_relevant_chunk_ids(
    store: SqliteStore,
    repo: str,
    commit: str,
    golden_list: List[str],
) -> Set[str]:
    rel: Set[str] = set()
    for g in golden_list:
        rel |= golden_to_relevant_chunk_ids(store, repo, commit, g)
    return rel


def run_spring_semantic_eval(args: argparse.Namespace) -> Dict[str, Any]:
    from .cli import _make_embedding_pipeline, _resolve_embedding_version

    store = SqliteStore(args.db)
    try:
        pipeline = _make_embedding_pipeline(store, args)
        emb_ver = (
            str(args.embedding_version).strip()
            if getattr(args, "embedding_version", None)
            else _resolve_embedding_version(args)
        )
        top_k = int(args.top_k)
        data = json.loads(Path(args.dataset).read_text(encoding="utf-8"))
        samples: List[Dict[str, Any]] = data["samples"]
        repo = str(args.repo)
        commit = str(args.commit)

        success_at_k: List[float] = []
        chunk_hit_ratio: List[float] = []
        mrrs: List[float] = []
        ndcgs: List[float] = []
        per_query: List[Dict[str, Any]] = []

        for idx, sample in enumerate(samples):
            q = sample["query"]
            golden: List[str] = sample.get("golden", [])
            relevant = sample_relevant_chunk_ids(store, repo, commit, golden)
            hits = pipeline.semantic_search(q, emb_ver, top_k)
            result_ids = [cid for cid, _ in hits]

            def _retrieved_rows() -> List[Dict[str, Any]]:
                rows: List[Dict[str, Any]] = []
                for rank, (cid, score) in enumerate(hits, start=1):
                    meta = store.fetch_chunk_metadata(cid, include_content=False)
                    rows.append(
                        {
                            "rank": rank,
                            "chunk_id": cid,
                            "score": float(score) if score is not None else None,
                            "path": (meta or {}).get("path"),
                            "start_line": (meta or {}).get("start_line"),
                            "end_line": (meta or {}).get("end_line"),
                            "language": (meta or {}).get("language"),
                            "is_relevant": cid in relevant if relevant else False,
                        }
                    )
                return rows

            if not relevant:
                per_query.append(
                    {
                        "index": idx,
                        "query": q,
                        "golden": golden,
                        "warning": "empty_relevant_set_after_fqcn_resolve",
                        "relevant_chunk_count": 0,
                        "success@k": 0.0,
                        "chunk_hit_ratio@k": 0.0,
                        "mrr": 0.0,
                        "ndcg@10": 0.0,
                        "retrieved": _retrieved_rows(),
                        "hits_in_top_k": 0,
                    }
                )
                success_at_k.append(0.0)
                chunk_hit_ratio.append(0.0)
                mrrs.append(0.0)
                ndcgs.append(0.0)
                continue

            retrieved = _retrieved_rows()

            hit_count = sum(1 for rid in result_ids if rid in relevant)
            success_at_k.append(1.0 if hit_count > 0 else 0.0)
            chunk_hit_ratio.append(hit_count / max(1, len(relevant)))

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
            ndcgs.append(0.0 if idcg == 0 else dcg / idcg)

            per_query.append(
                {
                    "index": idx,
                    "query": q,
                    "golden": golden,
                    "relevant_chunk_count": len(relevant),
                    "success@k": 1.0 if hit_count > 0 else 0.0,
                    "chunk_hit_ratio@k": hit_count / max(1, len(relevant)),
                    "mrr": rr,
                    "ndcg@10": (0.0 if idcg == 0 else dcg / idcg),
                    "retrieved": retrieved,
                    "hits_in_top_k": hit_count,
                }
            )

        n = max(1, len(success_at_k))
        return {
            "note": "per_query: golden 为人工标注；retrieved 为 semantic top-k，is_relevant 由 golden 自动展开 chunk 集合判定，供核对时参考。",
            "mode": "semantic",
            "embedding_version": emb_ver,
            "top_k": top_k,
            "repo": repo,
            "commit": commit,
            "samples": len(samples),
            "metrics": {
                "success@k": round(sum(success_at_k) / n, 4),
                "chunk_hit_ratio@k_mean": round(sum(chunk_hit_ratio) / n, 6),
                "mrr": round(sum(mrrs) / n, 4),
                "ndcg@10": round(sum(ndcgs) / n, 4),
            },
            "per_query": per_query,
        }
    finally:
        store.close()
