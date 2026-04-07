"""index_metadata 注册表 round-trip。"""

import json
from pathlib import Path

from hybrid_platform.index_metadata import (
    load_metadata,
    remove_entry,
    render_nginx_gateway_conf,
    save_metadata,
    upsert_entry,
)
from hybrid_platform.index_slug import index_db_path


def test_upsert_list_remove_roundtrip(tmp_path: Path) -> None:
    meta_file = tmp_path / "index_metadata.json"
    repo = "org/example"
    commit = "a" * 40
    db = index_db_path(repo, commit, tmp_path / "out")
    db.parent.mkdir(parents=True, exist_ok=True)
    db.write_bytes(b"")

    upsert_entry(
        repo,
        commit,
        str(db),
        config_path=str(tmp_path / "cfg.json"),
        metadata_path=meta_file,
    )
    meta = load_metadata(meta_file)
    assert len(meta.entries) == 1
    e = meta.entries[0]
    assert e.repo == repo
    assert e.commit == commit
    assert Path(e.db_path) == db.resolve()
    assert e.mcp_path.startswith("/mcp/")

    assert remove_entry(e.slug, metadata_path=meta_file)
    assert load_metadata(meta_file).entries == []


def test_render_nginx_uses_backend_host_for_fastmcp(tmp_path: Path) -> None:
    """公网 Host 转发到 127.0.0.1 后端时须改写 Host，否则 FastMCP DNS 校验返回 421。"""
    repo = "org/ex"
    commit = "b" * 40
    db = index_db_path(repo, commit, tmp_path / "o")
    db.parent.mkdir(parents=True, exist_ok=True)
    db.write_bytes(b"")
    upsert_entry(repo, commit, str(db), metadata_path=tmp_path / "m.json")
    meta = load_metadata(tmp_path / "m.json")
    conf = render_nginx_gateway_conf(meta.entries, listen_port=8765, backend_base_port=28065)
    assert "proxy_set_header Host 127.0.0.1:28065;" in conf
    assert "proxy_set_header X-Forwarded-Host $host;" in conf


def test_save_load_empty(tmp_path: Path) -> None:
    meta_file = tmp_path / "m.json"
    from hybrid_platform.index_metadata import IndexMetadataFile

    save_metadata(IndexMetadataFile(), meta_file)
    data = json.loads(meta_file.read_text(encoding="utf-8"))
    assert data["version"] == 1
    assert data["entries"] == []
