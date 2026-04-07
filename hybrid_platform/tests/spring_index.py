"""Spring integration tests: resolve index DB path from env or common mount paths."""

from __future__ import annotations

import os
from pathlib import Path


def spring_index_db_path() -> str | None:
    """Return path to Spring .db if present, else None (tests skip)."""
    env = (os.environ.get("HYBRID_SPRING_TEST_DB") or "").strip()
    if env and Path(env).is_file():
        return str(Path(env).resolve())
    for p in ("/data1/qadong/spring_v6.2.10.db", "/data/qadong/spring_v6.2.10.db"):
        if Path(p).is_file():
            return str(Path(p).resolve())
    return None


def spring_test_config_path() -> str:
    """Config path for Spring tests; default MCP config, override with HYBRID_SPRING_TEST_CONFIG."""
    env = (os.environ.get("HYBRID_SPRING_TEST_CONFIG") or "").strip()
    if env and Path(env).is_file():
        return str(Path(env).resolve())
    root = Path(__file__).resolve().parents[1]
    return str(root / "config" / "default_config.json")


def sqlite_has_table(db_path: str, table: str) -> bool:
    import sqlite3

    con = sqlite3.connect(db_path)
    try:
        row = con.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1",
            (table,),
        ).fetchone()
        return row is not None
    finally:
        con.close()
