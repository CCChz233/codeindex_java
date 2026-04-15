"""Java 全链路索引：scip-java → ingest → build-code-graph → chunk → embed（供 CLI 与管理 API 复用）。"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Callable

from .code_graph import CodeGraphBuilder
from .config import AppConfig
from .fallback_indexer import DocumentFallbackIndexer, SyntaxFallbackIndexer
from .ingestion import IngestionPipeline
from .index_contract import (
    FALLBACK_MODE_DOCUMENT,
    FALLBACK_MODE_OFF,
    FALLBACK_MODE_SYNTAX,
    SOURCE_MODE_DOCUMENT,
    SOURCE_MODE_SCIP,
    SOURCE_MODE_SYNTAX,
    normalize_fallback_mode,
)
from .java_indexer import JavaIndexRequest, JavaIndexer
from .runtime_factory import (
    chunk_runtime_dict_from_app_config,
    default_embedding_version_from_app_config,
    make_embedding_pipeline_from_app_config,
    make_vector_stores,
    vector_runtime_dict_from_app_config,
)
from .storage import SqliteStore
from .vector_store import SqliteVectorStore


def configure_vector_delete_hook_from_config(store: SqliteStore, cfg: AppConfig) -> None:
    _, write_stores = make_vector_stores(store, vector_runtime_dict_from_app_config(cfg))
    non_sqlite = [vs for vs in write_stores if not isinstance(vs, SqliteVectorStore)]
    if not non_sqlite:
        store.set_vector_delete_hook(None)
        return

    def _hook(chunk_ids: list[str]) -> None:
        for vector_store in non_sqlite:
            vector_store.delete_by_chunk_ids(chunk_ids, embedding_version=None)

    store.set_vector_delete_hook(_hook)


def load_app_config_for_build(*, config_path: str | None, config_inline: dict[str, Any] | None) -> AppConfig:
    if config_inline is not None:
        return AppConfig.merge_with_defaults(config_inline)
    if not config_path or not str(config_path).strip():
        raise ValueError("必须提供 config_path 或 config（内联 JSON 对象）")
    p = Path(config_path)
    if not p.is_file():
        raise ValueError(f"配置文件不存在: {config_path}")
    return AppConfig.load(str(p))


def resolve_build_paths(
    repo_root: str,
    db_path: str,
    *,
    allow_prefixes_raw: str | None = None,
) -> tuple[Path, Path]:
    root = Path(repo_root).resolve()
    db = Path(db_path).resolve()
    if not root.is_absolute() or not db.is_absolute():
        raise ValueError("repo_root 与 db_path 须为绝对路径")
    if not root.is_dir():
        raise ValueError(f"repo_root 不是目录: {root}")
    parent = db.parent
    if not parent.exists():
        raise ValueError(f"db_path 父目录不存在: {parent}")

    raw = (allow_prefixes_raw or os.environ.get("HYBRID_ADMIN_INDEX_ALLOW_PREFIXES", "") or "").strip()
    if raw:
        prefixes = [Path(x.strip()).resolve() for x in raw.split(",") if x.strip()]

        def _under_any(path: Path) -> bool:
            for pref in prefixes:
                try:
                    path.relative_to(pref)
                    return True
                except ValueError:
                    continue
            return False

        if not _under_any(root):
            raise ValueError("repo_root 不在 HYBRID_ADMIN_INDEX_ALLOW_PREFIXES 允许的路径下")
        if not _under_any(db):
            raise ValueError("db_path 不在 HYBRID_ADMIN_INDEX_ALLOW_PREFIXES 允许的路径下")

    return root, db


def run_java_full_index_pipeline(
    *,
    repo_root: str,
    repo: str,
    commit: str,
    db_path: str,
    config_path: str | None = None,
    config_inline: dict[str, Any] | None = None,
    serve_db_path: str | None = None,
    progress_callback: Callable[[str], None] | None = None,
    build_args: tuple[str, ...] = (),
) -> dict[str, Any]:
    """
    执行与 CLI ``index-java`` + ``build-code-graph`` + ``chunk`` + ``embed`` 等价的流水线。
    ``repo_root`` 须已在目标 ``commit`` 的工作树状态（本函数不执行 git checkout）。

    调用图在 ingest 之后、chunk 之前构建，以便 ``include_call_graph_context`` 等逻辑能读到 ``code_edges``。
    """
    cfg = load_app_config_for_build(config_path=config_path, config_inline=config_inline)
    root, db = resolve_build_paths(repo_root, db_path)

    if serve_db_path:
        try:
            if Path(db_path).resolve() == Path(serve_db_path).resolve():
                raise ValueError(
                    "db_path 与当前 serve 正在使用的库相同，并行写入可能导致损坏；请使用其它路径构建后切换"
                )
        except OSError:
            pass

    jcfg = cfg.get_section("java_index")
    out_rel = str(jcfg.get("output", "index.scip") or "index.scip").strip() or "index.scip"
    scip_out = Path(out_rel)
    if not scip_out.is_absolute():
        scip_out = root / scip_out

    def _emit(msg: str) -> None:
        if progress_callback is not None:
            progress_callback(msg)

    java_req = JavaIndexRequest(
        repo_root=str(root),
        output_path=str(scip_out),
        scip_java_cmd=str(jcfg.get("scip_java_cmd", "scip-java") or "scip-java").strip(),
        build_tool=str(jcfg.get("build_tool", "") or "").strip(),
        targetroot=str(jcfg.get("targetroot", "") or "").strip(),
        cleanup=bool(jcfg.get("cleanup", True)),
        verbose=bool(jcfg.get("verbose", False)),
        build_args=tuple(str(x) for x in build_args),
        semanticdb_targetroot=str(jcfg.get("semanticdb_targetroot", "") or "").strip(),
    )
    fallback_mode = normalize_fallback_mode(str(jcfg.get("fallback_mode", "syntax") or "syntax"))
    _emit("phase=pipeline.stage stage=scip_java status=start")
    source_mode = SOURCE_MODE_SCIP
    build_failure: dict[str, Any] | None = None
    fallback_stats: dict[str, Any] | None = None
    java_res = None
    try:
        java_res = JavaIndexer(java_req).run()
        scip_java_stats = {
            "build_tool": java_res.build_tool,
            "command": java_res.command,
            "output_path": java_res.output_path,
            "elapsed_ms": java_res.elapsed_ms,
            "used_manual_fallback": java_res.used_manual_fallback,
            "fallback_mode": fallback_mode,
        }
    except Exception as exc:
        scip_java_stats = {
            "build_tool": java_req.build_tool or "",
            "command": [],
            "output_path": str(scip_out),
            "elapsed_ms": 0,
            "used_manual_fallback": False,
            "fallback_mode": fallback_mode,
            "failed": True,
        }
        build_failure = {
            "type": type(exc).__name__,
            "message": str(exc),
        }
        if fallback_mode == FALLBACK_MODE_OFF:
            raise
    _emit("phase=pipeline.stage stage=scip_java status=done")

    ingest_section = cfg.get_section("ingest")
    batch_size = int(ingest_section.get("batch_size", 1000))
    index_version = str(ingest_section.get("index_version", "v1"))
    retries = int(ingest_section.get("retries", 2))
    source_root = str(ingest_section.get("source_root", "") or "").strip() or str(root)

    store = SqliteStore(str(db))
    try:
        configure_vector_delete_hook_from_config(store, cfg)
        _emit("phase=pipeline.stage stage=ingest status=start")
        if java_res is not None:
            store.prepare_index(
                repo,
                commit,
                source_mode=SOURCE_MODE_SCIP,
                build_tool=java_res.build_tool,
                build_failure=build_failure,
            )
            ingest_stats = IngestionPipeline(store, batch_size=batch_size).run(
                input_path=java_res.output_path,
                repo=repo,
                commit=commit,
                index_version=index_version,
                retries=retries,
                source_root=source_root,
                source_mode=SOURCE_MODE_SCIP,
                build_tool=java_res.build_tool,
                build_failure=build_failure,
            )
            ingest_dict = ingest_stats.__dict__
        else:
            fallback_errors: list[str] = []
            if fallback_mode == FALLBACK_MODE_SYNTAX:
                try:
                    store.prepare_index(
                        repo,
                        commit,
                        source_mode=SOURCE_MODE_SYNTAX,
                        build_tool=str(java_req.build_tool or ""),
                        build_failure=build_failure,
                    )
                    syntax_stats = SyntaxFallbackIndexer(store).run(str(root), repo, commit)
                    source_mode = SOURCE_MODE_SYNTAX
                    fallback_stats = syntax_stats.as_dict()
                except Exception as exc:
                    fallback_errors.append(str(exc))
            if fallback_stats is None:
                store.prepare_index(
                    repo,
                    commit,
                    source_mode=SOURCE_MODE_DOCUMENT,
                    build_tool=str(java_req.build_tool or ""),
                    build_failure=build_failure,
                )
                document_stats = DocumentFallbackIndexer(store).run(str(root), repo, commit)
                source_mode = SOURCE_MODE_DOCUMENT
                fallback_stats = document_stats.as_dict()
                if fallback_errors:
                    fallback_stats["syntax_error"] = fallback_errors[-1]
            ingest_dict = {
                "documents": int(fallback_stats.get("documents", 0)),
                "symbols": int(fallback_stats.get("symbols", 0)),
                "occurrences": int(fallback_stats.get("occurrences", 0)),
                "relations": int(fallback_stats.get("relations", 0)),
                "failures": 0,
                "source_mode": source_mode,
            }
        _emit("phase=pipeline.stage stage=ingest status=done")

        if store.get_source_mode() == SOURCE_MODE_DOCUMENT:
            graph_stats = CodeGraphBuilder(store).build(repo=repo, commit=commit)
        else:
            _emit("phase=pipeline.stage stage=build_code_graph status=start")
            graph_stats = CodeGraphBuilder(store).build(repo=repo, commit=commit)
            _emit("phase=pipeline.stage stage=build_code_graph status=done")

        embedding_version = default_embedding_version_from_app_config(cfg)
        chunk_kw = chunk_runtime_dict_from_app_config(cfg)

        pipeline = make_embedding_pipeline_from_app_config(store, cfg, progress_callback=_emit)
        _emit("phase=pipeline.stage stage=chunk status=start")
        chunks_total = pipeline.build_chunks(
            repo=repo,
            commit=commit,
            embedding_version=embedding_version,
            **chunk_kw,
        )
        _emit("phase=pipeline.stage stage=chunk status=done")
        _emit("phase=pipeline.stage stage=embed status=start")
        embed_stats = pipeline.run(embedding_version=embedding_version)
        _emit("phase=pipeline.stage stage=embed status=done")

        return {
            "ok": True,
            "repo_root": str(root),
            "db_path": str(db),
            "repo": repo,
            "commit": commit,
            "source_mode": store.get_source_mode(),
            "scip_java": scip_java_stats,
            "ingest": ingest_dict,
            "code_graph": graph_stats.__dict__,
            "chunk": {"chunks": chunks_total, "embedding_version": embedding_version},
            "embed": embed_stats.as_dict(),
            "index_info": store.get_index_info(),
            "fallback": fallback_stats,
        }
    finally:
        store.close()
