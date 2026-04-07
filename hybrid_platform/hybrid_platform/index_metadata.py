"""已构建索引（repo+commit）的注册表：供多 MCP 后端 + 单端口 Nginx 反代使用。"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .index_slug import index_db_path, mcp_http_path, repo_commit_slug


def default_metadata_path() -> Path:
    return Path(__file__).resolve().parents[1] / "var" / "index_metadata.json"


@dataclass
class IndexMetadataEntry:
    slug: str
    repo: str
    commit: str
    db_path: str
    mcp_path: str
    config_path: str
    status: str = "ready"
    updated_at: str = ""

    def __post_init__(self) -> None:
        if not self.updated_at:
            self.updated_at = datetime.now(timezone.utc).isoformat()


@dataclass
class IndexMetadataFile:
    version: int = 1
    entries: list[IndexMetadataEntry] = field(default_factory=list)

    def to_json_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "entries": [asdict(e) for e in self.entries],
        }

    @classmethod
    def from_json_dict(cls, data: dict[str, Any]) -> IndexMetadataFile:
        ver = int(data.get("version", 1))
        raw = data.get("entries") or []
        entries: list[IndexMetadataEntry] = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            entries.append(
                IndexMetadataEntry(
                    slug=str(item["slug"]),
                    repo=str(item["repo"]),
                    commit=str(item["commit"]),
                    db_path=str(item["db_path"]),
                    mcp_path=str(item["mcp_path"]),
                    config_path=str(item.get("config_path", "")),
                    status=str(item.get("status", "ready")),
                    updated_at=str(item.get("updated_at", "")),
                )
            )
        return cls(version=ver, entries=entries)


def load_metadata(path: Path | None = None) -> IndexMetadataFile:
    p = path or default_metadata_path()
    if not p.is_file():
        return IndexMetadataFile()
    with open(p, encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        return IndexMetadataFile()
    return IndexMetadataFile.from_json_dict(data)


def save_metadata(meta: IndexMetadataFile, path: Path | None = None) -> None:
    p = path or default_metadata_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(meta.to_json_dict(), f, ensure_ascii=False, indent=2)
    tmp.replace(p)


def upsert_entry(
    repo: str,
    commit: str,
    db_path: str,
    *,
    config_path: str = "",
    metadata_path: Path | None = None,
) -> IndexMetadataEntry:
    slug = repo_commit_slug(repo, commit)
    mpath = mcp_http_path(repo, commit)
    abs_db = str(Path(db_path).resolve())
    meta = load_metadata(metadata_path)
    new_e = IndexMetadataEntry(
        slug=slug,
        repo=repo.strip(),
        commit=commit.strip().lower(),
        db_path=abs_db,
        mcp_path=mpath,
        config_path=str(Path(config_path).resolve()) if config_path else "",
        status="ready",
    )
    kept = [e for e in meta.entries if e.slug != slug]
    kept.append(new_e)
    meta.entries = sorted(kept, key=lambda x: x.slug)
    save_metadata(meta, metadata_path)
    return new_e


def remove_entry(slug: str, *, metadata_path: Path | None = None) -> bool:
    meta = load_metadata(metadata_path)
    n = len(meta.entries)
    meta.entries = [e for e in meta.entries if e.slug != slug]
    if len(meta.entries) == n:
        return False
    save_metadata(meta, metadata_path)
    return True


def render_nginx_gateway_conf(
    entries: list[IndexMetadataEntry],
    *,
    listen_port: int = 8765,
    backend_base_port: int = 28065,
) -> str:
    """为每条 metadata 分配 backend_base_port+i，生成监听 listen_port 的 Nginx 配置片段（完整最小 http{}）。"""
    ready = [e for e in entries if e.status == "ready" and Path(e.db_path).is_file()]
    ready = sorted(ready, key=lambda x: x.slug)
    lines = [
        "worker_processes auto;",
        "error_log logs/error.log warn;",
        "pid logs/nginx.pid;",
        "events { worker_connections 1024; }",
        "http {",
        f"  server {{",
        f"    listen {int(listen_port)};",
        "    client_max_body_size 100m;",
    ]
    for i, e in enumerate(ready):
        port = backend_base_port + i
        # 路径与 HYBRID_MCP_PATH 一致，无前缀 location 匹配
        loc = e.mcp_path.rstrip("/") or "/mcp"
        lines.append(f"    location {loc} {{")
        lines.append(f"      proxy_pass http://127.0.0.1:{port};")
        lines.append("      proxy_http_version 1.1;")
        # FastMCP 在 host=127.0.0.1 时仅允许 Host: 127.0.0.1:*，转发公网 Host 会 421
        lines.append(f"      proxy_set_header Host 127.0.0.1:{port};")
        lines.append("      proxy_set_header X-Forwarded-Host $host;")
        lines.append("      proxy_set_header X-Real-IP $remote_addr;")
        lines.append("      proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;")
        lines.append("      proxy_set_header X-Forwarded-Proto $scheme;")
        lines.append('      proxy_set_header Connection "";')
        lines.append("      proxy_buffering off;")
        lines.append("      proxy_request_buffering off;")
        lines.append("      proxy_read_timeout 3600s;")
        lines.append("      proxy_send_timeout 3600s;")
        lines.append("    }")
    lines.append("  }")
    lines.append("}")
    return "\n".join(lines) + "\n"


def cli_assign_backend_ports(
    entries: list[IndexMetadataEntry],
    *,
    backend_base_port: int = 28065,
) -> list[tuple[IndexMetadataEntry, int]]:
    ready = [e for e in entries if e.status == "ready" and Path(e.db_path).is_file()]
    ready = sorted(ready, key=lambda x: x.slug)
    return [(e, backend_base_port + i) for i, e in enumerate(ready)]


def _metadata_path_arg(path: str) -> Path | None:
    return Path(path).resolve() if (path or "").strip() else None


def _cmd_upsert(args: argparse.Namespace) -> None:
    db = args.db
    if not db:
        od = (args.output_dir or "").strip() or None
        db = str(index_db_path(args.repo, args.commit, od))
    upsert_entry(
        args.repo,
        args.commit,
        db,
        config_path=args.config or "",
        metadata_path=_metadata_path_arg(args.metadata_file),
    )
    print(json.dumps({"ok": True, "db_path": str(Path(db).resolve())}, ensure_ascii=False))


def _cmd_list(args: argparse.Namespace) -> None:
    meta = load_metadata(_metadata_path_arg(args.metadata_file))
    print(json.dumps(meta.to_json_dict(), ensure_ascii=False, indent=2))


def _cmd_remove(args: argparse.Namespace) -> None:
    ok = remove_entry(args.slug, metadata_path=_metadata_path_arg(args.metadata_file))
    print(json.dumps({"ok": ok}, ensure_ascii=False))


def _cmd_nginx_conf(args: argparse.Namespace) -> None:
    meta = load_metadata(_metadata_path_arg(args.metadata_file))
    text = render_nginx_gateway_conf(
        meta.entries,
        listen_port=args.listen,
        backend_base_port=args.backend_base,
    )
    sys.stdout.write(text)


def _cmd_backend_map(args: argparse.Namespace) -> None:
    meta = load_metadata(_metadata_path_arg(args.metadata_file))
    pairs = cli_assign_backend_ports(meta.entries, backend_base_port=args.backend_base)
    out = [
        {
            "slug": e.slug,
            "db_path": e.db_path,
            "mcp_path": e.mcp_path,
            "config_path": e.config_path,
            "internal_port": port,
        }
        for e, port in pairs
    ]
    print(json.dumps(out, ensure_ascii=False, indent=2))


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(description="index metadata registry for multi-MCP gateway")
    p.add_argument(
        "--metadata-file",
        default="",
        help="默认 hybrid_platform/var/index_metadata.json",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    u = sub.add_parser("upsert", help="注册或更新一条已构建索引")
    u.add_argument("--repo", required=True)
    u.add_argument("--commit", required=True)
    u.add_argument("--db", default="", help="默认按 output-dir + slug 推导")
    u.add_argument("--config", default="", help="MCP 启动用 HYBRID_CONFIG")
    u.add_argument("--output-dir", default="", help="与 index_build 的 output-dir 一致，用于推导 db")
    u.set_defaults(func=_cmd_upsert)

    sub.add_parser("list", help="打印 JSON").set_defaults(func=_cmd_list)

    r = sub.add_parser("remove", help="按 slug 删除")
    r.add_argument("--slug", required=True)
    r.set_defaults(func=_cmd_remove)

    g = sub.add_parser("nginx-conf", help="写出 Nginx 完整最小配置到 stdout")
    g.add_argument("--listen", type=int, default=8765)
    g.add_argument("--backend-base", type=int, default=28065)
    g.set_defaults(func=_cmd_nginx_conf)

    b = sub.add_parser("backend-map", help="JSON：每条 entry 与内网端口")
    b.add_argument("--backend-base", type=int, default=28065)
    b.set_defaults(func=_cmd_backend_map)

    args = p.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
