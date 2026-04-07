"""读取 spring_semantic_queries.jsonl（query + ground_truth.gold_files / gold_symbols），评测 semantic 检索。"""

from __future__ import annotations

import argparse
import json
import math
import re
from pathlib import Path
from typing import Any, Dict, List, Set

from .spring_semantic_eval import _dcg, sample_relevant_chunk_ids
from .storage import SqliteStore

_SPLIT_RE = re.compile(r"\s*\|\s*")


def _split_pipe_field(s: str | None) -> List[str]:
    if not s or not str(s).strip():
        return []
    return [p.strip() for p in _SPLIT_RE.split(str(s).strip()) if p.strip()]


def _ground_truth_str_list(val: Any) -> List[str]:
    """gold_files / gold_symbols：支持 JSON 数组，或 `a | b` 单字符串。"""
    if val is None:
        return []
    if isinstance(val, list):
        return [str(x).strip() for x in val if str(x).strip()]
    if isinstance(val, str):
        return _split_pipe_field(val)
    return _split_pipe_field(str(val))


def ground_truth_to_relevant_chunk_ids(
    store: SqliteStore,
    repo: str,
    commit: str,
    ground_truth: Dict[str, Any],
) -> Set[str]:
    rel: Set[str] = set()
    files = _ground_truth_str_list(ground_truth.get("gold_files"))
    if files:
        q_marks = ",".join("?" * len(files))
        params: List[Any] = [repo, commit, *files]
        cur = store.conn.execute(
            f"""
            SELECT document_id FROM documents
            WHERE repo = ? AND commit_hash = ? AND relative_path IN ({q_marks})
            """,
            params,
        )
        doc_ids = [str(r[0]) for r in cur.fetchall()]
        for doc_id in doc_ids:
            cur2 = store.conn.execute(
                "SELECT chunk_id FROM chunks WHERE document_id = ?",
                (doc_id,),
            )
            rel.update(str(r[0]) for r in cur2.fetchall())

    symbols = _ground_truth_str_list(ground_truth.get("gold_symbols"))
    for sym in symbols:
        rel |= sample_relevant_chunk_ids(store, repo, commit, [sym])

    return rel


def run_spring_jsonl_semantic_eval(args: argparse.Namespace) -> Dict[str, Any]:
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
        repo = str(args.repo)
        commit = str(args.commit)
        jsonl_path = Path(args.jsonl)

        success_at_k: List[float] = []
        chunk_hit_ratio: List[float] = []
        mrrs: List[float] = []
        ndcgs: List[float] = []
        per_query: List[Dict[str, Any]] = []
        idx = 0

        with jsonl_path.open(encoding="utf-8") as f:
            for line_no, line in enumerate(f, start=1):
                line = line.strip()
                if not line:
                    continue
                row = json.loads(line)
                q = row["query"]
                gt = row.get("ground_truth") or {}

                relevant = ground_truth_to_relevant_chunk_ids(store, repo, commit, gt)
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
                            "line": line_no,
                            "index": idx,
                            "query": q,
                            "ground_truth": gt,
                            "warning": "empty_relevant_set",
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
                    idx += 1
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
                ndcg = 0.0 if idcg == 0 else dcg / idcg
                ndcgs.append(ndcg)

                per_query.append(
                    {
                        "line": line_no,
                        "index": idx,
                        "query": q,
                        "ground_truth": gt,
                        "relevant_chunk_count": len(relevant),
                        "success@k": 1.0 if hit_count > 0 else 0.0,
                        "chunk_hit_ratio@k": hit_count / max(1, len(relevant)),
                        "mrr": rr,
                        "ndcg@10": ndcg,
                        "retrieved": retrieved,
                        "hits_in_top_k": hit_count,
                    }
                )
                idx += 1

        nq = len(success_at_k)
        if nq == 0:
            metrics = {
                "success@k": 0.0,
                "chunk_hit_ratio@k_mean": 0.0,
                "mrr": 0.0,
                "ndcg@10": 0.0,
            }
        else:
            metrics = {
                "success@k": round(sum(success_at_k) / nq, 4),
                "chunk_hit_ratio@k_mean": round(sum(chunk_hit_ratio) / nq, 6),
                "mrr": round(sum(mrrs) / nq, 4),
                "ndcg@10": round(sum(ndcgs) / nq, 4),
            }
        return {
            "note": "jsonl: gold_files / gold_symbols 展开为相关 chunk；empty_relevant_set 请先用 prune-spring-jsonl 从数据集中删掉。",
            "jsonl": str(jsonl_path.resolve()),
            "mode": "semantic",
            "embedding_version": emb_ver,
            "top_k": top_k,
            "repo": repo,
            "commit": commit,
            "samples": len(per_query),
            "metrics": metrics,
            "per_query": per_query,
        }
    finally:
        store.close()


def run_prune_spring_jsonl(args: argparse.Namespace) -> Dict[str, Any]:
    """按当前 db/repo/commit 的 ground_truth 解析结果，从 JSONL 中删掉 relevant 为空的行。"""
    store = SqliteStore(args.db)
    try:
        repo = str(args.repo)
        commit = str(args.commit)
        inp = Path(args.input_jsonl)
        outp = Path(args.output_jsonl)
        kept = 0
        dropped = 0
        dropped_line_numbers: List[int] = []
        out_buf: List[str] = []
        with inp.open(encoding="utf-8") as f:
            for line_no, line in enumerate(f, start=1):
                s = line.strip()
                if not s:
                    continue
                row = json.loads(s)
                gt = row.get("ground_truth") or {}
                rel = ground_truth_to_relevant_chunk_ids(store, repo, commit, gt)
                if not rel:
                    dropped += 1
                    dropped_line_numbers.append(line_no)
                    continue
                out_buf.append(s)
                kept += 1
        outp.parent.mkdir(parents=True, exist_ok=True)
        with outp.open("w", encoding="utf-8") as w:
            for s in out_buf:
                w.write(s + "\n")
        return {
            "input": str(inp.resolve()),
            "output": str(outp.resolve()),
            "repo": repo,
            "commit": commit,
            "kept": kept,
            "dropped": dropped,
            "dropped_line_numbers": dropped_line_numbers,
        }
    finally:
        store.close()
