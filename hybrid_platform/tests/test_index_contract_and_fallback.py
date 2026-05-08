from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from hybrid_platform.agent_mcp_handlers import CodeindexMcpRuntime
from hybrid_platform.dsl import Query
from hybrid_platform.entity_query import find_entity
from hybrid_platform.index_contract import capabilities_for_source_mode
from hybrid_platform.index_build_runner import run_java_full_index_pipeline
from hybrid_platform.index_contract import ReindexRequiredError, SnapshotMismatchError, UnsupportedCapabilityError
from hybrid_platform.retrieval import HybridRetrievalService
from hybrid_platform.source_indexer import SourceIndexResult
from hybrid_platform.storage import SqliteStore


def _write_config(tmp_path: Path, *, fallback_mode: str, source_backend: str = "") -> str:
    java_index = {
        "scip_java_cmd": "false",
        "fallback_mode": fallback_mode,
    }
    if source_backend:
        java_index["source_backend"] = source_backend
    cfg = {
        "embedding": {"provider": "deterministic", "dim": 64, "version": "v1"},
        "vector": {"backend": "sqlite", "write_mode": "sqlite_only", "lancedb": {}},
        "java_index": java_index,
    }
    suffix = source_backend.replace("-", "_") or fallback_mode
    path = tmp_path / f"cfg-{suffix}.json"
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
    assert result["source_backend"] == "tree-sitter-java"
    assert result["index_info"]["source_backend"] == "tree-sitter-java"
    assert "call" in result["index_info"]["capabilities"]
    assert "ref" in result["index_info"]["capabilities"]
    store = SqliteStore(str(db_path))
    try:
        entities = find_entity(store, type="class", name="App", match="exact")
        assert entities, "syntax fallback should index class declarations"

        service = HybridRetrievalService(store)
        defs = service.def_of(entities[0].symbol_id, top_k=5)
        assert defs, "syntax fallback should expose definition locations"
        service.callees_of(entities[0].symbol_id, top_k=5)
    finally:
        store.close()

    runtime = CodeindexMcpRuntime(str(db_path), config_path)
    try:
        symbol = json.loads(runtime.handle_find_symbol("class", "App", match="exact"))
        assert symbol["ok"] is True
        graph = json.loads(runtime.handle_symbol_graph("callees_of", symbol["entities"][0]["symbol_id"]))
        assert graph["ok"] is True
    finally:
        runtime.close()


def test_explicit_tree_sitter_backend_does_not_call_java_indexer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = Path(__file__).resolve().parents[1]
    repo_root = root / "examples" / "java-smoke"
    config_path = _write_config(tmp_path, fallback_mode="off", source_backend="tree-sitter-java")
    db_path = tmp_path / "tree-selected.db"
    called = {"tree": False}

    class FailingJavaIndexer:
        def __init__(self, request: object) -> None:
            raise AssertionError("JavaIndexer must not be constructed for tree-sitter-java backend")

    class FakeTreeSitterIndexer:
        def __init__(self, *, repo_root: str, repo: str, commit: str, build_failure: object | None = None) -> None:
            self.repo = repo
            self.commit = commit

        def run(self, store: SqliteStore) -> SourceIndexResult:
            called["tree"] = True
            stats = {"source_mode": "syntax", "documents": 0, "symbols": 0, "occurrences": 0, "relations": 0}
            store.prepare_index(
                self.repo,
                self.commit,
                source_mode="syntax",
                build_tool="tree-sitter-java",
                source_backend="tree-sitter-java",
                backend_version="test",
                backend_stats=stats,
                capabilities=capabilities_for_source_mode("syntax"),
            )
            return SourceIndexResult(
                source_backend="tree-sitter-java",
                source_mode="syntax",
                ingest={**stats, "failures": 0},
                backend_stats=stats,
            )

    monkeypatch.setattr("hybrid_platform.index_build_runner.JavaIndexer", FailingJavaIndexer)
    monkeypatch.setattr("hybrid_platform.index_build_runner.TreeSitterJavaSourceIndexer", FakeTreeSitterIndexer)

    result = run_java_full_index_pipeline(
        repo_root=str(repo_root),
        repo="demo/java-smoke",
        commit="feedfacefeedfacefeedfacefeedfacefeedface",
        db_path=str(db_path),
        config_path=config_path,
    )

    assert called["tree"] is True
    assert result["source_backend"] == "tree-sitter-java"
    assert result["index_info"]["source_backend"] == "tree-sitter-java"
    assert "call" in result["index_info"]["capabilities"]


def test_explicit_scip_backend_does_not_fallback_to_syntax(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[1]
    repo_root = root / "examples" / "java-smoke"
    config_path = _write_config(tmp_path, fallback_mode="syntax", source_backend="scip-java")
    db_path = tmp_path / "scip-selected.db"

    with pytest.raises(RuntimeError, match="scip-java"):
        run_java_full_index_pipeline(
            repo_root=str(repo_root),
            repo="demo/java-smoke",
            commit="feedfacefeedfacefeedfacefeedfacefeedface",
            db_path=str(db_path),
            config_path=config_path,
        )


def test_tree_sitter_backend_indexes_project_calls_and_refs(tmp_path: Path) -> None:
    pytest.importorskip("tree_sitter")
    pytest.importorskip("tree_sitter_java")

    repo_root = tmp_path / "repo"
    src = repo_root / "src" / "main" / "java" / "demo"
    src.mkdir(parents=True)
    (src / "Service.java").write_text(
        """
        package demo;
        public class Service {
            public int total() {
                return 1;
            }
        }
        """,
        encoding="utf-8",
    )
    (src / "Runner.java").write_text(
        """
        package demo;
        public interface Runner {
            int run();
        }
        """,
        encoding="utf-8",
    )
    (src / "Autowired.java").write_text(
        """
        package demo;
        public @interface Autowired {
        }
        """,
        encoding="utf-8",
    )
    (src / "Bean.java").write_text(
        """
        package demo;
        public @interface Bean {
        }
        """,
        encoding="utf-8",
    )
    app_src = repo_root / "src" / "main" / "java" / "demo" / "app"
    app_src.mkdir(parents=True)
    (app_src / "App.java").write_text(
        """
        package demo.app;
        import demo.Autowired;
        import demo.Bean;
        import demo.Runner;
        import demo.Service;
        public class App implements Runner {
            @Autowired
            private Service service;
            public App(@Autowired Service service) {
                this.service = service;
            }
            @Bean
            public int run() {
                return service.total();
            }
        }
        """,
        encoding="utf-8",
    )
    config_path = _write_config(tmp_path, fallback_mode="off", source_backend="tree-sitter-java")
    db_path = tmp_path / "tree-quality.db"

    result = run_java_full_index_pipeline(
        repo_root=str(repo_root),
        repo="demo/tree-quality",
        commit="cafebabecafebabecafebabecafebabecafebabe",
        db_path=str(db_path),
        config_path=config_path,
    )

    assert result["source_backend"] == "tree-sitter-java"
    store = SqliteStore(str(db_path))
    try:
        service_cls = find_entity(store, type="class", name="Service", match="exact")[0]
        total_method = find_entity(store, type="method", name="total", match="exact")[0]
        app_method = [
            item
            for item in find_entity(store, type="method", name="run", match="exact")
            if "App.run" in item.symbol_id
        ][0]
        service_field = find_entity(store, type="field", name="service", match="exact")[0]
        autowired = find_entity(store, type="annotation", name="Autowired", match="exact")[0]
        bean = find_entity(store, type="annotation", name="Bean", match="exact")[0]
        assert find_entity(store, type="package", name="demo.app", match="exact")
        assert find_entity(store, type="import", name="demo.Service", match="exact")

        refs = HybridRetrievalService(store).refs_of(total_method.symbol_id, top_k=5)
        assert any((r.payload or {}).get("path", "").endswith("App.java") for r in refs)
        annotation_refs = HybridRetrievalService(store).refs_of(autowired.symbol_id, top_k=5)
        assert any((r.payload or {}).get("path", "").endswith("App.java") for r in annotation_refs)
        callers = HybridRetrievalService(store).callers_of(total_method.symbol_id, top_k=5)
        assert any(r.result_id == app_method.symbol_id for r in callers)

        row = store.conn.execute(
            """
            SELECT 1 FROM relations
            WHERE from_symbol = ? AND to_symbol = ? AND relation_type = 'field_refs'
            LIMIT 1
            """,
            (app_method.symbol_id, service_field.symbol_id),
        ).fetchone()
        assert row is not None
        annotated_field = store.conn.execute(
            """
            SELECT 1 FROM relations
            WHERE from_symbol = ? AND to_symbol = ? AND relation_type = 'annotated_with'
            LIMIT 1
            """,
            (service_field.symbol_id, autowired.symbol_id),
        ).fetchone()
        assert annotated_field is not None
        annotated_method = store.conn.execute(
            """
            SELECT 1 FROM relations
            WHERE from_symbol = ? AND to_symbol = ? AND relation_type = 'annotated_with'
            LIMIT 1
            """,
            (app_method.symbol_id, bean.symbol_id),
        ).fetchone()
        assert annotated_method is not None
        field_card = store.conn.execute(
            """
            SELECT content FROM chunks
            WHERE primary_symbol_ids LIKE ? AND chunk_id LIKE '%:symbol_card:%'
            LIMIT 1
            """,
            (f"%{service_field.symbol_id}%",),
        ).fetchone()
        assert field_card is not None
        assert "annotations: @Autowired" in str(field_card["content"])
        assert "field_type: Service" in str(field_card["content"])
        method_chunk = store.conn.execute(
            """
            SELECT content FROM chunks
            WHERE primary_symbol_ids LIKE ? AND chunk_id NOT LIKE '%:symbol_card:%'
            LIMIT 1
            """,
            (f"%{app_method.symbol_id}%",),
        ).fetchone()
        assert method_chunk is not None
        assert "path: src/main/java/demo/app/App.java" in str(method_chunk["content"])
        assert "annotations: @Bean" in str(method_chunk["content"])
        assert service_cls.symbol_id
    finally:
        store.close()
