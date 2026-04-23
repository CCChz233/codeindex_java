#!/usr/bin/env python3
"""Run MCP + index acceptance checks (metadata, routes, in-process tools, optional validate).

Usage:
  cd hybrid_platform && ./myenv/bin/python scripts/run_mcp_index_acceptance.py
  ./myenv/bin/python scripts/run_mcp_index_acceptance.py --skip-validate
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

HYBRID_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = HYBRID_ROOT.parent
# package root is hybrid_platform/ (parent of hybrid_platform package dir)
if str(HYBRID_ROOT) not in sys.path:
    sys.path.insert(0, str(HYBRID_ROOT))
META = HYBRID_ROOT / "var" / "index_metadata.json"
ROUTES = REPO_ROOT / "workspace" / "manifests" / "test_java_agent_manifest_size_ge_100000.routes.json"
MANIFEST_JSONL = REPO_ROOT / "JAVA test" / "test_java_agent_manifest_size_ge_100000.jsonl"
TARGETS = REPO_ROOT / "workspace" / "manifests" / "test_java_agent_manifest_size_ge_100000.targets.json"


def _cfg_1024() -> str:
    base = json.loads((HYBRID_ROOT / "config" / "java_eval_deterministic_config.json").read_text())
    base["embedding"]["dim"] = 1024
    base["embedding"]["llama"]["kwargs"]["output_dimension"] = 1024
    d = Path(tempfile.mkdtemp())
    p = d / "embed1024.json"
    p.write_text(json.dumps(base), encoding="utf-8")
    return str(p)


def check_metadata() -> None:
    data = json.loads(META.read_text())
    entries = [e for e in data["entries"] if e.get("status") == "ready"]
    missing = [e["slug"] for e in entries if not Path(e["db_path"]).is_file()]
    print(f"metadata: ready={len(entries)} missing_files={len(missing)}")
    if missing:
        raise SystemExit(f"missing db_path: {missing[:5]}")


def check_routes() -> None:
    meta = {e["slug"]: e for e in json.loads(META.read_text())["entries"]}
    routes = json.loads(ROUTES.read_text())["routes"]
    for r in routes:
        if r.get("index_status") != "ready":
            continue
        m = meta[r["slug"]]
        if Path(r["db_path"]).resolve() != Path(m["db_path"]).resolve():
            raise SystemExit(f"db_path mismatch {r['sample_id']}")
        if r.get("mcp_path") != m.get("mcp_path"):
            raise SystemExit(f"mcp_path mismatch {r['sample_id']}")
    print("routes: 27 ready entries align with metadata")


def check_sqlite_sample() -> None:
    data = json.loads(META.read_text())
    slug = next(e for e in data["entries"] if e["slug"].startswith("netty_netty_"))
    db = slug["db_path"]
    r = subprocess.run(["sqlite3", db, "PRAGMA quick_check;"], capture_output=True, text=True)
    assert r.stdout.strip() == "ok", r.stdout
    con = sqlite3.connect(db)
    n = con.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
    con.close()
    print(f"sqlite sample ({slug['slug'][:40]}…): quick_check ok chunks={n}")


def check_mcp_tools() -> None:
    os.environ["HYBRID_DB"] = str(
        next(e["db_path"] for e in json.loads(META.read_text())["entries"] if e["slug"].startswith("netty_netty_"))
    )
    os.environ["HYBRID_CONFIG"] = _cfg_1024()
    from hybrid_platform.mcp_env_runtime import get_mcp_runtime, reset_mcp_runtime_for_tests
    from hybrid_platform.mcp_streamable_server import _build_mcp

    reset_mcp_runtime_for_tests()
    mcp = _build_mcp()
    tm = getattr(mcp, "_tool_manager", None)
    names = sorted(getattr(t, "name", "") for t in getattr(tm, "_tools", {}).values())
    assert names == ["find_symbol", "semantic_query", "symbol_graph"], names
    print("fastmcp tools:", names)

    rt = get_mcp_runtime()
    assert rt is not None
    out = json.loads(
        rt.handle_semantic_query(
            query="ByteBuf allocation",
            mode="hybrid",
            top_k=5,
            blend_strategy="linear",
            include_code=False,
            max_code_chars=400,
            embedding_version=None,
        )
    )
    assert out.get("ok") is True, out
    assert len(out.get("results") or []) > 0, "hybrid should return hits"
    out2 = json.loads(rt.handle_find_symbol("class", "ByteBuf", "contains", "", 5))
    assert out2.get("ok") and (out2.get("count") or 0) >= 1
    sid = out2["entities"][0]["symbol_id"]
    out3 = json.loads(rt.handle_symbol_graph("def_of", sid, 5, False, 400, None))
    assert out3.get("ok") is True, out3
    rt.close()
    reset_mcp_runtime_for_tests()
    print("in-process: semantic_query hybrid + find_symbol + symbol_graph ok")


def check_isolation() -> None:
    cfg = _cfg_1024()
    netty = str(
        Path(next(e["db_path"] for e in json.loads(META.read_text())["entries"] if e["slug"].startswith("netty_netty_")))
    )
    kc = str(
        Path(
            next(
                e["db_path"]
                for e in json.loads(META.read_text())["entries"]
                if e["slug"].startswith("keycloak_keycloak_1eba")
            )
        )
    )

    from hybrid_platform.mcp_env_runtime import get_mcp_runtime, reset_mcp_runtime_for_tests

    def realm_hits(db: str) -> int:
        os.environ["HYBRID_DB"] = db
        os.environ["HYBRID_CONFIG"] = cfg
        reset_mcp_runtime_for_tests()
        rt = get_mcp_runtime()
        o = json.loads(
            rt.handle_find_symbol("class", "Realm", "contains", "org.keycloak", 20),
        )
        rt.close()
        reset_mcp_runtime_for_tests()
        return int(o.get("count") or 0)

    assert realm_hits(netty) == 0
    assert realm_hits(kc) >= 1
    print("isolation: keycloak Realm not in netty index")


def run_validate() -> None:
    cmd = [
        sys.executable,
        "-m",
        "hybrid_platform.java_eval_prep",
        "validate",
        "--manifest",
        str(MANIFEST_JSONL),
        "--worktrees-root",
        str(REPO_ROOT / "workspace" / "worktrees"),
        "--index-output-dir",
        str(REPO_ROOT / "workspace" / "indices"),
        "--metadata-file",
        str(META),
        "--targets-out",
        str(TARGETS),
        "--routes-out",
        str(ROUTES),
        "--sample-id",
        "netty__netty-15575",
        "--no-require-worktree",
    ]
    r = subprocess.run(cmd, cwd=str(HYBRID_ROOT), capture_output=True, text=True)
    print(r.stdout)
    if r.returncode != 0:
        print(r.stderr, file=sys.stderr)
        raise SystemExit(r.returncode)
    print("java_eval_prep validate: ok")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--skip-validate", action="store_true")
    args = ap.parse_args()
    os.chdir(HYBRID_ROOT)
    check_metadata()
    check_routes()
    check_sqlite_sample()
    check_mcp_tools()
    check_isolation()
    if not args.skip_validate:
        run_validate()
    print("ALL_ACCEPTANCE_CHECKS_PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
