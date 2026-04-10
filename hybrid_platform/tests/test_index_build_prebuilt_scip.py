"""index_build_repo_commit.sh在 --prebuilt-scip 下跳过 scip-java，仅 ingest 及后续阶段。"""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.fixture
def minimal_config(tmp_path: Path) -> str:
    import json

    cfg = {
        "embedding": {"provider": "deterministic", "dim": 128, "version": "v1"},
        "vector": {"backend": "sqlite", "write_mode": "sqlite_only", "lancedb": {}},
    }
    p = tmp_path / "cfg.json"
    p.write_text(json.dumps(cfg), encoding="utf-8")
    return str(p)


def test_index_build_repo_commit_prebuilt_scip_ingests_only(
    tmp_path: Path, minimal_config: str
) -> None:
    hybrid_root = Path(__file__).resolve().parents[1]
    script = hybrid_root / "scripts" / "index_build_repo_commit.sh"
    smoke = hybrid_root / "examples" / "java-smoke"
    scip = smoke / "index.scip"
    assert scip.is_file(), f"missing fixture {scip}"

    out_dir = tmp_path / "indices"
    out_dir.mkdir()
    env = dict(os.environ)
    env["SKIP_CODE_GRAPH"] = "1"
    env["SKIP_CHUNK"] = "1"
    env["SKIP_EMBED"] = "1"
    env["HYBRID_PYTHON"] = sys.executable

    commit = "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef"
    cmd = [
        "bash",
        str(script),
        "--config",
        minimal_config,
        "--repo-name",
        "demo/java-smoke",
        "--commit",
        commit,
        "--repo-root",
        str(smoke),
        "--output-dir",
        str(out_dir),
        "--prebuilt-scip",
        str(scip),
    ]
    r = subprocess.run(cmd, cwd=str(hybrid_root), env=env, capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr

    from hybrid_platform.index_slug import repo_commit_slug

    slug = repo_commit_slug("demo/java-smoke", commit)
    db_path = out_dir / f"{slug}.db"
    assert db_path.is_file(), f"expected db at {db_path}"

    con = sqlite3.connect(str(db_path))
    try:
        n = con.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
        assert int(n) > 0
    finally:
        con.close()
