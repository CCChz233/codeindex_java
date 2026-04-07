from __future__ import annotations

import json
from pathlib import Path

import pytest

REPO = "demo/repo"
COMMIT = "abc123"


@pytest.fixture(scope="session")
def sample_ndjson_path() -> Path:
    root = Path(__file__).resolve().parents[1]
    p = root / "examples" / "sample.scip.ndjson"
    assert p.is_file(), f"missing {p}"
    return p


@pytest.fixture
def test_config_path(tmp_path: Path) -> str:
    overrides = {
        "embedding": {"provider": "deterministic", "dim": 128, "version": "v1"},
        "vector": {"backend": "sqlite", "write_mode": "sqlite_only", "lancedb": {}},
    }
    p = tmp_path / "mcp_test_config.json"
    p.write_text(json.dumps(overrides), encoding="utf-8")
    return str(p)


def build_fixture_database(db_path: Path, ndjson: Path, config_path: str) -> None:
    from hybrid_platform.code_graph import CodeGraphBuilder
    from hybrid_platform.config import AppConfig
    from hybrid_platform.ingestion import IngestionPipeline
    from hybrid_platform.runtime_factory import (
        chunk_runtime_dict_from_app_config,
        default_embedding_version_from_app_config,
        make_embedding_pipeline_from_app_config,
    )
    from hybrid_platform.storage import SqliteStore

    cfg = AppConfig.load(config_path)
    store = SqliteStore(str(db_path))
    try:
        IngestionPipeline(store).run(
            input_path=str(ndjson),
            repo=REPO,
            commit=COMMIT,
            source_root="",
        )
        pipe = make_embedding_pipeline_from_app_config(store, cfg)
        ver = default_embedding_version_from_app_config(cfg)
        ck = chunk_runtime_dict_from_app_config(cfg)
        pipe.build_chunks(repo=REPO, commit=COMMIT, embedding_version=ver, **ck)
        pipe.run(ver)
        CodeGraphBuilder(store).build(repo=REPO, commit=COMMIT)
    finally:
        store.close()


@pytest.fixture
def mcp_fixture_db(tmp_path: Path, sample_ndjson_path: Path, test_config_path: str) -> tuple[str, str]:
    db = tmp_path / "fixture.db"
    build_fixture_database(db, sample_ndjson_path, test_config_path)
    return str(db), test_config_path
