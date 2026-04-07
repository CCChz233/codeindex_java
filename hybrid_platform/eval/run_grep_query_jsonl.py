#!/usr/bin/env python3
"""
对 examples/grep_query.jsonl 跑 rg/grep baseline：按命中行顺序去重得到「文件排名」，算 success@k 与 MRR。

- gold：ground_truth.gold_files（相对仓库根的路径，与 JSONL 一致）
- success@k：至少一个 gold 文件出现在前 k 个**不同文件**中则为 1，否则 0
- mrr@{k}（截断 MRR）：仅当首个 gold 的 rank ≤ k 时为 1/rank，否则 0（与 success@k 一致：top-k 外则 0）
- mrr（全列表 MRR）：不截断；首个 gold 在第 200 个文件则 RR=1/200，故会出现「success@k=0 但 mrr>0」

用法：
  cd hybrid_platform
  python eval/run_grep_query_jsonl.py \\
    --repo-root /path/to/spring-framework \\
    --jsonl examples/grep_query.jsonl \\
    --top-k 10 \\
    -o /tmp/grep_baseline_metrics.json
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Sequence, Set


def _ground_truth_files(gt: Dict[str, Any]) -> List[str]:
    raw = gt.get("gold_files")
    if raw is None:
        return []
    if isinstance(raw, list):
        return [str(x).strip().replace("\\", "/") for x in raw if str(x).strip()]
    if isinstance(raw, str):
        return [p.strip().replace("\\", "/") for p in raw.split("|") if p.strip()]
    return []


def _resolve_rel_path(repo_root: Path, path_part: str) -> str:
    p = Path(path_part)
    if not p.is_absolute():
        p = repo_root / p
    try:
        return p.resolve().relative_to(repo_root.resolve()).as_posix()
    except ValueError:
        return p.resolve().as_posix()


def _ordered_files_rg(repo_root: Path, pattern: str, timeout_s: int) -> Tuple[List[str], str]:
    rg = shutil.which("rg")
    if not rg:
        return [], "rg_not_found"
    cmd = [
        rg,
        "-n",
        "--glob",
        "*.java",
        "--no-heading",
        "--color",
        "never",
        pattern,
        str(repo_root.resolve()),
    ]
    proc = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=timeout_s,
    )
    if proc.returncode not in (0, 1):
        return [], f"rg_exit_{proc.returncode}"
    ordered: List[str] = []
    seen: Set[str] = set()
    root = repo_root.resolve()
    for line in (proc.stdout or "").splitlines():
        line = line.strip()
        if not line:
            continue
        # path:line:content — Spring 路径通常无冒号；首段为路径
        head, _, _ = line.partition(":")
        if not head:
            continue
        p = Path(head)
        if not p.is_absolute():
            p = root / head
        try:
            rel = p.resolve().relative_to(root).as_posix()
        except ValueError:
            continue
        if rel not in seen:
            seen.add(rel)
            ordered.append(rel)
    return ordered, "rg"


def _ordered_files_grep(repo_root: Path, pattern: str, timeout_s: int) -> Tuple[List[str], str]:
    grep = shutil.which("grep")
    if not grep:
        return [], "grep_not_found"
    cmd = [
        grep,
        "-RIn",
        "-E",
        "--include=*.java",
        pattern,
        str(repo_root.resolve()),
    ]
    proc = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=timeout_s,
    )
    if proc.returncode not in (0, 1):
        return [], f"grep_exit_{proc.returncode}"
    ordered: List[str] = []
    seen: Set[str] = set()
    root = repo_root.resolve()
    for line in (proc.stdout or "").splitlines():
        line = line.strip()
        if not line:
            continue
        head, _, _ = line.partition(":")
        if not head:
            continue
        p = Path(head)
        if not p.is_absolute():
            p = root / head
        try:
            rel = p.resolve().relative_to(root).as_posix()
        except ValueError:
            continue
        if rel not in seen:
            seen.add(rel)
            ordered.append(rel)
    return ordered, "grep"


def _metrics_for_query(
    ordered_files: Sequence[str],
    gold_rel: Sequence[str],
    top_k: int,
) -> Dict[str, Any]:
    gold_norm = {g.replace("\\", "/") for g in gold_rel if g.strip()}
    rank_by_file = {f: i + 1 for i, f in enumerate(ordered_files)}
    ranks = sorted(rank_by_file[g] for g in gold_norm if g in rank_by_file)
    min_rank = ranks[0] if ranks else None
    mrr_full = (1.0 / min_rank) if min_rank else 0.0
    success = 1.0 if (min_rank is not None and min_rank <= top_k) else 0.0
    mrr_at_k = (1.0 / min_rank) if (min_rank is not None and min_rank <= top_k) else 0.0
    return {
        "mrr": mrr_full,
        f"mrr@{top_k}": mrr_at_k,
        f"success@{top_k}": success,
        "min_relevant_rank": min_rank,
        "gold_files": sorted(gold_norm),
        "hits_total_unique_files": len(ordered_files),
    }


def run(
    repo_root: Path,
    jsonl_path: Path,
    top_k: int,
    timeout_s: int,
) -> Dict[str, Any]:
    k = max(1, int(top_k))
    repo_root = repo_root.resolve()
    if not repo_root.is_dir():
        raise FileNotFoundError(f"repo_root 不是目录: {repo_root}")

    mrrs_full: List[float] = []
    mrrs_at_k: List[float] = []
    successes: List[float] = []
    per_query: List[Dict[str, Any]] = []

    with jsonl_path.open(encoding="utf-8") as f:
        for line_no, line in enumerate(f, start=1):
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            qid = row.get("id", line_no)
            pattern = (row.get("grep_regex") or "").strip()
            gt = row.get("ground_truth") or {}
            gold_files = _ground_truth_files(gt)

            if not pattern:
                per_query.append(
                    {
                        "id": qid,
                        "line": line_no,
                        "skip": True,
                        "reason": "empty_grep_regex",
                    }
                )
                continue
            if not gold_files:
                per_query.append(
                    {
                        "id": qid,
                        "line": line_no,
                        "skip": True,
                        "reason": "empty_gold_files",
                    }
                )
                continue

            ordered, tool = _ordered_files_rg(repo_root, pattern, timeout_s)
            if tool != "rg":
                ordered, tool = _ordered_files_grep(repo_root, pattern, timeout_s)

            m = _metrics_for_query(ordered, gold_files, k)
            mrrs_full.append(float(m["mrr"]))
            mrrs_at_k.append(float(m[f"mrr@{k}"]))
            successes.append(float(m[f"success@{k}"]))

            per_query.append(
                {
                    "id": qid,
                    "line": line_no,
                    "query": row.get("query", ""),
                    "grep_regex": pattern,
                    "tool": tool,
                    "top_k": k,
                    "mrr": round(m["mrr"], 6),
                    f"mrr@{k}": round(m[f"mrr@{k}"], 6),
                    f"success@{k}": m[f"success@{k}"],
                    "min_relevant_rank": m["min_relevant_rank"],
                    "hits_total_unique_files": m["hits_total_unique_files"],
                    "gold_files": m["gold_files"],
                    "ranked_files_head": ordered[: max(k, 20)],
                }
            )

    n = max(1, len(mrrs_full))
    return {
        "repo_root": str(repo_root),
        "jsonl": str(jsonl_path.resolve()),
        "top_k": k,
        "samples": len(mrrs_full),
        "metrics": {
            f"mrr@{k}": round(sum(mrrs_at_k) / n, 6),
            "mrr": round(sum(mrrs_full) / n, 6),
            f"success@{k}": round(sum(successes) / n, 6),
            "note": f"mrr@{k} 与 success@{k} 对齐：仅 rank≤k 时非零。mrr 为全列表不截断。",
        },
        "per_query": per_query,
    }


def main(argv: Sequence[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="grep_query.jsonl baseline：success@k、mrr@k（截断）、mrr（全列表）")
    p.add_argument("--repo-root", required=True, help="Spring 源码根目录（含 spring-core 等模块）")
    p.add_argument("--jsonl", required=True, help="grep_query.jsonl 路径")
    p.add_argument("--top-k", type=int, default=10, help="success@k 的 k（默认 10）")
    p.add_argument(
        "--timeout",
        type=int,
        default=180,
        help="单条 query 的 rg/grep 超时秒数",
    )
    p.add_argument("-o", "--output", default=None, help="可选：完整 JSON 写入文件")
    args = p.parse_args(list(argv) if argv is not None else None)

    report = run(
        Path(args.repo_root),
        Path(args.jsonl),
        top_k=args.top_k,
        timeout_s=args.timeout,
    )
    text = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        outp = Path(args.output)
        outp.parent.mkdir(parents=True, exist_ok=True)
        outp.write_text(text, encoding="utf-8")
    summary = {
        "top_k": report["top_k"],
        "samples": report["samples"],
        "metrics": report["metrics"],
        "jsonl": report["jsonl"],
        "repo_root": report["repo_root"],
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    if args.output:
        print(f"(full report written to {args.output})", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
