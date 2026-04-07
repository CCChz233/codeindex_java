"""索引与 MCP 子路径命名：``{sanitized_repo}_{commit_sha}``（全小写 hex commit）。"""

from __future__ import annotations

import re
import sys
from pathlib import Path


def sanitize_repo_name(repo: str) -> str:
    """将逻辑仓库名转为路径/文件名安全片段（不含 commit）。"""
    s = (repo or "").strip()
    if not s:
        return "repo"
    s = s.replace("\\", "/")
    parts = [p for p in s.split("/") if p and p not in (".", "..")]
    if not parts:
        return "repo"
    joined = "_".join(parts)
    joined = re.sub(r"[^a-zA-Z0-9._-]+", "_", joined)
    joined = re.sub(r"_+", "_", joined).strip("_")
    if not joined:
        return "repo"
    if len(joined) > 200:
        joined = joined[:200].rstrip("_")
    return joined


def normalize_commit_sha(commit_sha: str) -> str:
    commit = "".join((commit_sha or "").strip().split()).lower()
    if not re.fullmatch(r"[0-9a-f]{7,40}", commit):
        raise ValueError(
            "commit_sha must be a lowercase hex string of length 7–40 (got invalid or wrong case)"
        )
    return commit


def repo_commit_slug(repo: str, commit_sha: str) -> str:
    """``repo_commitsha`` 单段 slug，用于 ``.db`` 文件名与 ``/mcp/<slug>``。"""
    return f"{sanitize_repo_name(repo)}_{normalize_commit_sha(commit_sha)}"


def default_index_dir() -> Path:
    return Path(__file__).resolve().parents[1] / "var" / "hybrid_indices"


def index_db_path(repo: str, commit_sha: str, output_dir: str | Path | None = None) -> Path:
    root = Path(output_dir) if output_dir is not None else default_index_dir()
    slug = repo_commit_slug(repo, commit_sha)
    return root / f"{slug}.db"


def mcp_http_path(repo: str, commit_sha: str, prefix: str = "/mcp") -> str:
    p = (prefix or "/mcp").strip()
    if not p.startswith("/"):
        p = "/" + p
    p = p.rstrip("/")
    return f"{p}/{repo_commit_slug(repo, commit_sha)}"


def main(argv: list[str] | None = None) -> None:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) < 2:
        print(
            "usage: python -m hybrid_platform.index_slug <repo_name> <commit_sha> [--db-dir DIR]",
            file=sys.stderr,
        )
        sys.exit(2)
    repo = args[0]
    commit = args[1]
    db_dir = None
    if len(args) >= 4 and args[2] == "--db-dir":
        db_dir = args[3]
    slug = repo_commit_slug(repo, commit)
    print(slug)
    if db_dir is not None:
        print(str(Path(db_dir) / f"{slug}.db"))
    else:
        print(str(index_db_path(repo, commit)))
    print(mcp_http_path(repo, commit))


if __name__ == "__main__":
    main()
