"""用源码树上的 grep/rg 作为朴素 baseline，与 ``find_entity``（索引）对比。

baseline 策略（模拟「在仓库里搜类名/方法名」）：

- ``class`` / ``interface`` / ``enum``：在 ``*.java`` 中搜 ``class Name`` / ``interface Name``。
- ``method``：在 ``package_contains`` 推导出的 glob（如 ``*DefaultChannelPipeline*.java``）内搜 ``\\bname\\b``；
  若无 ``package_contains``，则全局 ``*.java`` 搜方法名（噪声会很大，结果里会标出）。
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple


def symbol_id_to_java_suffix(symbol_id: str) -> Optional[str]:
    """从 SCIP symbol_id 抽出 ``org/.../Foo.java`` / ``io/netty/...`` 等源文件相对路径后缀。"""
    if "#" not in symbol_id:
        return None
    # 常见 Java 源码根（与 Gradle/Maven 的 src/main/java 下包路径一致）
    roots = ("org/", "com/", "io/", "javax/", "java/")
    idx = -1
    for r in roots:
        j = symbol_id.find(r)
        if j != -1 and (idx == -1 or j < idx):
            idx = j
    if idx == -1:
        return None
    rest = symbol_id[idx:]
    base = rest.split("#", 1)[0].strip()
    if not base:
        return None
    if base.endswith(".java"):
        return base
    return base + ".java"


def resolve_expected_paths(repo_root: Path, symbol_id: str) -> List[Path]:
    """在仓库中解析 ``symbol_id`` 对应的源文件路径（可能 0/1/多个模块）。"""
    suffix = symbol_id_to_java_suffix(symbol_id)
    if not suffix:
        return []
    tail = suffix.split("/")[-1]  # AbstractByteBuf.java
    out: List[Path] = []
    for p in repo_root.rglob(tail):
        try:
            rel = p.relative_to(repo_root)
        except ValueError:
            continue
        if str(rel).replace("\\", "/").endswith(suffix):
            out.append(p.resolve())
    return out


def _which_rg() -> Optional[str]:
    return shutil.which("rg")


def _rg_files_with_pattern(
    repo_root: Path,
    pattern: str,
    glob_pat: Optional[str],
) -> Set[Path]:
    """``rg -l`` 返回匹配到的文件绝对路径集合。"""
    rg = _which_rg()
    if not rg:
        return _grep_r_files(repo_root, pattern, glob_pat)

    cmd: List[str] = [rg, "-l", "--glob", "*.java", pattern, str(repo_root)]
    if glob_pat:
        cmd = [rg, "-l", "--glob", glob_pat, pattern, str(repo_root)]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    if proc.returncode not in (0, 1):
        return set()
    paths: Set[Path] = set()
    for line in (proc.stdout or "").splitlines():
        line = line.strip()
        if not line:
            continue
        paths.add(Path(line).resolve())
    return paths


def _grep_r_files(
    repo_root: Path,
    pattern: str,
    glob_pat: Optional[str],
) -> Set[Path]:
    """无 rg 时用 grep -r -l（较慢）。"""
    grep = shutil.which("grep")
    if not grep:
        return set()
    inc = "*.java"
    if glob_pat:
        inc = glob_pat.replace("*", "")
    cmd = [
        grep,
        "-r",
        "-l",
        "-E",
        pattern,
        "--include=" + (glob_pat or "*.java"),
        str(repo_root),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
    if proc.returncode not in (0, 1):
        return set()
    paths: Set[Path] = set()
    for line in (proc.stdout or "").splitlines():
        line = line.strip()
        if line:
            paths.add(Path(line).resolve())
    return paths


def _method_glob_from_package_contains(package_contains: str) -> Optional[str]:
    """从 ``package_contains`` 得到 ``rg --glob`` 片段，如 ``DefaultChannelPipeline`` -> ``*DefaultChannelPipeline*.java``。"""
    pc = (package_contains or "").strip()
    if not pc:
        return None
    pc = pc.rstrip("#")
    # 取最后一段标识符
    token = re.split(r"[/:#.]", pc)
    token = [t for t in token if t and t not in ("java", "io")]
    if not token:
        return None
    name = max(token, key=len)
    if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", name):
        return f"*{name}*.java"
    return f"*{name}*.java"


def entity_query_to_grep_spec(eq: Dict[str, Any]) -> Tuple[str, Optional[str]]:
    """返回 (regex_pattern, rg_glob_or_none)。"""
    t = (eq.get("type") or "any").lower()
    name = eq.get("name") or ""
    pc = eq.get("package_contains") or ""

    if t == "class":
        return (rf"class\s+{re.escape(name)}\b", None)
    if t == "interface":
        return (rf"interface\s+{re.escape(name)}\b", None)
    if t == "enum":
        return (rf"enum\s+{re.escape(name)}\b", None)
    if t == "method":
        g = _method_glob_from_package_contains(pc)
        return (rf"\b{re.escape(name)}\b", g)
    if t == "type":
        return (rf"(class|interface|enum)\s+{re.escape(name)}\b", None)
    # any / field / constructor / variable：退化为名字子串
    return (re.escape(name), None)


@dataclass
class GrepBaselineQueryRow:
    index: int
    entity_query: Dict[str, Any]
    grep_pattern: str
    grep_glob: Optional[str]
    expected_paths: List[str]
    matched_paths: List[str]
    """rg/grep 命中的全部文件路径（字典序排序，稳定可复现）。"""
    matched_total_count: int
    matched_paths_top_k: List[str]
    """仅前 ``top_k`` 条，用于与 find_entity 的 ``top_k`` 对齐。"""
    top_k: int
    expected_count: int
    matched_expected_count: int
    grep_recall_files: float
    """不截断：命中文件中覆盖 GT 源文件的比例（与 ``grep_recall_at_k`` 不同）。"""
    grep_recall_at_k: float
    """只看排序后前 ``top_k`` 个文件时，GT 源文件被覆盖的比例（与 find_entity 的 recall@k 定义对齐）。"""
    extra_file_count: int
    tool: str


@dataclass
class GrepBaselineReport:
    queries: List[GrepBaselineQueryRow] = field(default_factory=list)
    top_k: int = 10
    mean_grep_recall_files: float = 0.0
    mean_grep_recall_at_k: float = 0.0
    total_extra_files: int = 0


def run_grep_baseline(repo_root: str, dataset_path: str, top_k: int = 10) -> GrepBaselineReport:
    root = Path(repo_root).resolve()
    if not root.is_dir():
        raise FileNotFoundError(f"repo_root 不是目录: {repo_root}")

    data = json.loads(Path(dataset_path).read_text(encoding="utf-8"))
    samples: List[Dict[str, Any]] = data["samples"]
    tool = "rg" if _which_rg() else ("grep" if shutil.which("grep") else "none")

    rows: List[GrepBaselineQueryRow] = []
    recalls: List[float] = []
    recalls_at_k: List[float] = []
    total_extra = 0
    k = max(1, int(top_k))

    for idx, sample in enumerate(samples):
        eq = sample["entity_query"]
        relevant = sample.get("relevant_ids", [])
        pattern, glob_pat = entity_query_to_grep_spec(eq)

        expected_resolved: Set[Path] = set()
        for sid in relevant:
            for p in resolve_expected_paths(root, sid):
                expected_resolved.add(p)
        expected_strs = sorted(str(p) for p in expected_resolved)
        expected_set_str = set(expected_strs)

        matched = _rg_files_with_pattern(root, pattern, glob_pat)
        matched_sorted = sorted(str(p) for p in matched)
        matched_top_k = matched_sorted[:k]
        matched_expected = {p for p in matched if p in expected_resolved}
        extra = matched - expected_resolved

        n_exp = len(expected_resolved)
        n_hit = len(matched_expected)
        rec = (n_hit / n_exp) if n_exp else 1.0
        recalls.append(rec)
        total_extra += len(extra)

        in_topk = sum(1 for ep in expected_set_str if ep in set(matched_top_k))
        rec_at_k = (in_topk / n_exp) if n_exp else 1.0
        recalls_at_k.append(rec_at_k)

        rows.append(
            GrepBaselineQueryRow(
                index=idx,
                entity_query=dict(eq),
                grep_pattern=pattern,
                grep_glob=glob_pat,
                expected_paths=expected_strs,
                matched_paths=matched_sorted,
                matched_total_count=len(matched_sorted),
                matched_paths_top_k=matched_top_k,
                top_k=k,
                expected_count=n_exp,
                matched_expected_count=n_hit,
                grep_recall_files=rec,
                grep_recall_at_k=rec_at_k,
                extra_file_count=len(extra),
                tool=tool,
            )
        )

    n = max(1, len(recalls))
    return GrepBaselineReport(
        queries=rows,
        top_k=k,
        mean_grep_recall_files=sum(recalls) / n,
        mean_grep_recall_at_k=sum(recalls_at_k) / n,
        total_extra_files=total_extra,
    )


def grep_baseline_report_to_json(
    report: GrepBaselineReport,
    find_entity_summary: Optional[Dict[str, float]] = None,
    find_entity_queries: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    k = report.top_k
    query_json: List[Dict[str, Any]] = []
    for q in report.queries:
        row: Dict[str, Any] = {
            "index": q.index,
            "entity_query": q.entity_query,
            "k": k,
            "grep_pattern": q.grep_pattern,
            "grep_glob": q.grep_glob,
            "expected_paths": q.expected_paths,
            "grep_matched_total_count": q.matched_total_count,
            "grep_matched_paths_top_k": q.matched_paths_top_k,
            "grep_recall_files": round(q.grep_recall_files, 4),
            "grep_recall@k": round(q.grep_recall_at_k, 4),
            "extra_file_count": q.extra_file_count,
        }
        if find_entity_queries is not None and q.index < len(find_entity_queries):
            fe = find_entity_queries[q.index]
            row["find_entity_returned_count"] = fe.get("returned_count")
            row["find_entity_recall@k"] = round(float(fe.get("recall@k", 0.0)), 4)
            row["relevant_symbol_count"] = len(fe.get("relevant_ids", []))
            row["gap_recall@k"] = round(
                float(fe.get("recall@k", 0.0)) - float(q.grep_recall_at_k),
                4,
            )
        query_json.append(row)

    out: Dict[str, Any] = {
        "tool": report.queries[0].tool if report.queries else "none",
        "k": k,
        "mean_find_entity_recall@k": find_entity_summary.get("recall@k") if find_entity_summary else None,
        "mean_grep_recall_files": round(report.mean_grep_recall_files, 4),
        "mean_grep_recall@k": round(report.mean_grep_recall_at_k, 4),
        "total_extra_files": report.total_extra_files,
        "queries": query_json,
    }
    if find_entity_summary:
        out["find_entity_summary"] = find_entity_summary
        fe = float(find_entity_summary.get("recall@k", 0.0))
        gr = report.mean_grep_recall_at_k
        out["gap"] = {
            "mean_find_entity_recall@k_minus_mean_grep_recall@k": round(fe - gr, 4),
            "find_entity_recall@k": round(fe, 4),
            "grep_mean_recall@k": round(gr, 4),
            "k": k,
            "grep_mean_recall_files_unbounded": round(report.mean_grep_recall_files, 4),
            "grep_total_extra_match_files": report.total_extra_files,
            "note": "两侧均使用同一 k：find_entity 为符号列表前 k 条上的 recall@k；grep 为按路径字典序取前 k 个**文件**后，GT 源文件被覆盖比例。grep_recall_files 为不截断时的文件级召回。",
        }
    return out
