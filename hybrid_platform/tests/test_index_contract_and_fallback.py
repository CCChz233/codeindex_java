from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from hybrid_platform.agent_mcp_handlers import CodeindexMcpRuntime
from hybrid_platform.dsl import Query
from hybrid_platform.entity_query import find_entity
from hybrid_platform.index_build_runner import run_java_full_index_pipeline
from hybrid_platform.index_contract import ReindexRequiredError, SnapshotMismatchError, UnsupportedCapabilityError
from hybrid_platform.retrieval import HybridRetrievalService
from hybrid_platform.storage import SqliteStore


def _write_config(tmp_path: Path, *, fallback_mode: str) -> str:
    cfg = {
        "embedding": {"provider": "deterministic", "dim": 64, "version": "v1"},
        "vector": {"backend": "sqlite", "write_mode": "sqlite_only", "lancedb": {}},
        "java_index": {
            "scip_java_cmd": "false",
            "fallback_mode": fallback_mode,
        },
    }
    path = tmp_path / f"cfg-{fallback_mode}.json"
    path.write_text(json.dumps(cfg), encoding="utf-8")
    return str(path)


def test_rejects_legacy_schema_without_index_info(tmp_path: Path) -> None:
    db = tmp_path / "legacy.db"
    con = sqlite3.connect(str(db))
    try:
        con.execute("CREATE TABLE documents(document_id TEXT PRIMARY KEY)")
        con.commit()
    finally:
        con.close()

    with pytest.raises(ReindexRequiredError):
        SqliteStore(str(db))


def test_single_snapshot_mismatch_rejected(tmp_path: Path) -> None:
    db = tmp_path / "single-snapshot.db"
    store = SqliteStore(str(db))
    try:
        store.prepare_index("demo/repo", "abc123", source_mode="scip")
        with pytest.raises(SnapshotMismatchError):
            store.prepare_index("demo/other", "deadbeef", source_mode="scip")
    finally:
        store.close()


def test_document_fallback_build_and_runtime(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[1]
    repo_root = root / "examples" / "java-smoke"
    config_path = _write_config(tmp_path, fallback_mode="document")
    db_path = tmp_path / "document.db"

    result = run_java_full_index_pipeline(
        repo_root=str(repo_root),
        repo="demo/java-smoke",
        commit="deadbeefdeadbeefdeadbeefdeadbeefdeadbeef",
        db_path=str(db_path),
        config_path=config_path,
    )

    assert result["source_mode"] == "document"
    assert result["index_info"]["source_mode"] == "document"

    store = SqliteStore(str(db_path))
    try:
        service = HybridRetrievalService(store)
        hits = service.query(Query(text="Hello SCIP", mode="hybrid", top_k=5))
        assert hits, "document fallback should still produce retrievable chunks"
        assert all((r.payload or {}).get("source_mode") == "document" for r in hits)
        with pytest.raises(UnsupportedCapabilityError):
            find_entity(store, type="class", name="App", match="exact")
    finally:
        store.close()

    runtime = CodeindexMcpRuntime(str(db_path), config_path)
    try:
        payload = json.loads(runtime.handle_find_symbol("class", "App", match="exact"))
        assert payload["ok"] is False
        assert payload["error"]["code"] == "UNSUPPORTED_CAPABILITY"
    finally:
        runtime.close()


def test_syntax_fallback_build_and_capabilities(tmp_path: Path) -> None:
    pytest.importorskip("tree_sitter")
    pytest.importorskip("tree_sitter_java")

    root = Path(__file__).resolve().parents[1]
    repo_root = root / "examples" / "java-smoke"
    config_path = _write_config(tmp_path, fallback_mode="syntax")
    db_path = tmp_path / "syntax.db"

    result = run_java_full_index_pipeline(
        repo_root=str(repo_root),
        repo="demo/java-smoke",
        commit="feedfacefeedfacefeedfacefeedfacefeedface",
        db_path=str(db_path),
        config_path=config_path,
    )

    assert result["source_mode"] == "syntax"
    store = SqliteStore(str(db_path))
    try:
        entities = find_entity(store, type="class", name="App", match="exact")
        assert entities, "syntax fallback should index class declarations"

        service = HybridRetrievalService(store)
        defs = service.def_of(entities[0].symbol_id, top_k=5)
        assert defs, "syntax fallback should expose definition locations"
        with pytest.raises(UnsupportedCapabilityError):
            service.callees_of(entities[0].symbol_id, top_k=5)
    finally:
        store.close()

    runtime = CodeindexMcpRuntime(str(db_path), config_path)
    try:
        symbol = json.loads(runtime.handle_find_symbol("class", "App", match="exact"))
        assert symbol["ok"] is True
        graph = json.loads(runtime.handle_symbol_graph("callees_of", symbol["entities"][0]["symbol_id"]))
        assert graph["ok"] is False
        assert graph["error"]["code"] == "UNSUPPORTED_CAPABILITY"
    finally:
        runtime.close()
