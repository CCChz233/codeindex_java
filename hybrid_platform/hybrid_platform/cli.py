from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any, Callable

try:
    from tqdm.auto import tqdm
except Exception:  # pragma: no cover
    tqdm = None

from .dsl import Query, callees_of as dsl_callees_of, callers_of as dsl_callers_of, def_of as dsl_def_of, refs_of as dsl_refs_of
from .embedding import EmbeddingPipeline, make_chunk_token_count_fn
from .eval import Evaluator
from .code_graph import CodeGraphBuilder
from .community import IntentCommunityBuilder
from .config import AppConfig
from .ingestion import IngestionPipeline
from .isolated_policy import IsolatedNodePolicy
from .intent_builder import FunctionIntentBuilder
from .retrieval import HybridRetrievalService
from .service_api import run_server
from .graph_service import GraphService
from .graph_eval import GraphEvaluator
from .repair_calls import CallsRepairer
from .java_indexer import JavaIndexRequest, JavaIndexer
from .entity_eval import entity_eval_report_to_json, format_entity_eval_metrics, run_entity_eval
from .grep_baseline import grep_baseline_report_to_json, run_grep_baseline
from .entity_query import entity_types, find_entity
from .runtime_factory import make_embedding_pipeline_from_app_config, make_vector_stores
from .storage import SqliteStore
from .vector_store import SqliteVectorStore
from .vector_store_lancedb import LanceDbVectorStore

DEFAULT_CONFIG_PATH = str(Path(__file__).resolve().parents[1] / "config" / "default_config.json")


def _print_json(data: Any) -> None:
    print(json.dumps(data, ensure_ascii=False, indent=2))


_PROGRESS_FIELD_RE = re.compile(r"([a-zA-Z_]+)=([^ ]+)")


def _parse_progress_fields(message: str) -> dict[str, str]:
    return {key: value for key, value in _PROGRESS_FIELD_RE.findall(message)}


def _parse_fraction(value: str) -> tuple[int, int] | None:
    left, sep, right = value.partition("/")
    if not sep:
        return None
    if not left.isdigit() or not right.isdigit():
        return None
    return int(left), int(right)


class _TqdmProgressReporter:
    def __init__(self, prefix: str) -> None:
        self.prefix = prefix
        self._enabled = tqdm is not None
        self._bar: Any | None = None

    def __call__(self, message: str) -> None:
        self.emit(message)

    def emit(self, message: str) -> None:
        line = f"[{self.prefix}] {message}"
        if self._enabled and self._handle_progress_message(message):
            return
        self._write(line)

    def close(self) -> None:
        if self._bar is not None:
            self._bar.close()
            self._bar = None

    def _write(self, line: str) -> None:
        if self._bar is not None and tqdm is not None:
            tqdm.write(_truncate_progress_line(line))
            return
        print(_truncate_progress_line(line), file=sys.stderr, flush=True)

    def _ensure_bar(self, total: int, *, desc: str, unit: str) -> None:
        total = max(0, int(total))
        target_desc = f"[{self.prefix}] {desc}"
        if self._bar is None:
            self._bar = tqdm(total=total, desc=target_desc, unit=unit, dynamic_ncols=True, leave=False)
            return
        if self._bar.total != total:
            self._bar.close()
            self._bar = tqdm(total=total, desc=target_desc, unit=unit, dynamic_ncols=True, leave=False)
            return
        self._bar.set_description_str(target_desc)

    def _set_progress(self, current: int, total: int) -> None:
        if self._bar is None:
            return
        current = max(0, min(int(current), int(total)))
        if current < self._bar.n:
            self._bar.n = current
            self._bar.refresh()
            return
        delta = current - int(self._bar.n)
        if delta > 0:
            self._bar.update(delta)

    def _set_postfix(self, text: str) -> None:
        if self._bar is not None:
            self._bar.set_postfix_str(_truncate_progress_line(text, limit=120), refresh=False)

    def _complete_bar(self) -> None:
        if self._bar is not None and self._bar.total is not None:
            total = int(self._bar.total)
            if int(self._bar.n) < total:
                self._bar.update(total - int(self._bar.n))

    def _handle_progress_message(self, message: str) -> bool:
        fields = _parse_progress_fields(message)
        if "phase=build_chunks.start" in message:
            total = int(fields.get("docs", "0") or 0)
            strategy = fields.get("strategy", "chunk")
            self._ensure_bar(total, desc=f"chunk {strategy}", unit="doc")
            filtered = fields.get("filtered_non_code_docs", "0")
            self._set_postfix(f"filtered={filtered}")
            return True
        if "phase=build_chunks.progress" in message:
            docs = _parse_fraction(fields.get("docs", ""))
            if docs is None:
                return False
            current, total = docs
            self._ensure_bar(total, desc="chunk", unit="doc")
            self._set_progress(current, total)
            postfix = f"chunks={fields.get('chunks', '0')}"
            path = fields.get("path", "")
            if path:
                postfix = f"{postfix} path={path}"
            self._set_postfix(postfix)
            return True
        if "phase=build_chunks.done" in message:
            total = int(fields.get("docs", "0") or 0)
            self._ensure_bar(total, desc="chunk", unit="doc")
            self._complete_bar()
            self.close()
            self._write(line=f"[{self.prefix}] {message}")
            return True
        if "phase=embed.start" in message:
            total = int(fields.get("batches", "0") or 0)
            provider = fields.get("provider", "embed")
            self._ensure_bar(total, desc=f"embed {provider}", unit="batch")
            postfix = (
                f"pending={fields.get('pending_chunks', '0')} "
                f"skipped={fields.get('skipped_chunks', '0')}"
            )
            self._set_postfix(postfix)
            return True
        if "phase=embed.progress" in message:
            batches = _parse_fraction(fields.get("batches", ""))
            if batches is None:
                return False
            current, total = batches
            self._ensure_bar(total, desc="embed", unit="batch")
            self._set_progress(current, total)
            self._set_postfix(
                " ".join(
                    [
                        f"embedded={fields.get('embedded_chunks', '0')}",
                        f"failed={fields.get('failed_batches', '0')}",
                        f"retried={fields.get('retried_batches', '0')}",
                    ]
                )
            )
            return True
        if "phase=embed.done" in message:
            total = int(fields.get("batches", "0") or 0)
            if total > 0:
                self._ensure_bar(total, desc="embed", unit="batch")
            self._complete_bar()
            self.close()
            self._write(line=f"[{self.prefix}] {message}")
            return True
        if "phase=embed.batch_failed" in message or "phase=embed.batch_retried" in message:
            self._write(f"[{self.prefix}] {message}")
            return True
        return False


def _truncate_progress_line(message: str, limit: int = 400) -> str:
    text = str(message).strip()
    if len(text) <= limit:
        return text
    return text[:limit] + "..."


def _progress_printer(prefix: str) -> Callable[[str], None]:
    return _TqdmProgressReporter(prefix)


def _cfg(args: argparse.Namespace) -> AppConfig:
    return args.app_config


def _resolve(args: argparse.Namespace, attr: str, section: str, key: str, fallback: Any = None) -> Any:
    value = getattr(args, attr, None)
    if value is not None:
        return value
    return _cfg(args).get(section, key, fallback)


def _format_results(results: list[Any]) -> list[dict[str, Any]]:
    return [
        {
            "id": r.result_id,
            "type": r.result_type,
            "score": round(r.score, 6),
            "explain": r.explain,
            "payload": r.payload,
        }
        for r in results
    ]


def _make_service(store: SqliteStore, args: argparse.Namespace) -> HybridRetrievalService:
    qcfg = _cfg(args).get_section("query")
    return HybridRetrievalService(
        store,
        embedding_pipeline=_make_embedding_pipeline(store, args),
        default_embedding_version=_resolve_embedding_version(args),
        test_code_depref_enabled=bool(qcfg.get("test_code_depref_enabled", True)),
        test_code_score_factor=float(qcfg.get("test_code_score_factor", 0.55)),
    )


def _chunk_token_count_fn_for_args(args: argparse.Namespace) -> Callable[[str], int]:
    chunk_cfg = _cfg(args).get_section("chunk")
    tc_backend = str(chunk_cfg.get("token_counter", "auto") or "auto").strip().lower()
    model = str(_cfg(args).get("embedding", "model", "")).strip()
    tc_model = str(chunk_cfg.get("token_counter_model") or "").strip() or model
    return make_chunk_token_count_fn(backend=tc_backend, model=tc_model or None)


def _make_embedding_pipeline(
    store: SqliteStore,
    args: argparse.Namespace,
    progress_callback: Callable[[str], None] | None = None,
) -> EmbeddingPipeline:
    return make_embedding_pipeline_from_app_config(
        store, _cfg(args), progress_callback=progress_callback
    )


def _resolve_embedding_version(args: argparse.Namespace) -> str:
    """优先 `--embedding-version`，否则读 `embedding.version`（配置）。"""
    return str(_resolve(args, "embedding_version", "embedding", "version", "v1"))


def _embedding_runtime_config(args: argparse.Namespace) -> dict[str, object]:
    cfg = _cfg(args)
    return {
        "provider": str(cfg.get("embedding", "provider", "deterministic")).strip().lower(),
        "model": str(cfg.get("embedding", "model", "deterministic-hash-v1")).strip(),
        "dim": int(cfg.get("embedding", "dim", 128)),
        "api_base": str(cfg.get("embedding", "api_base", "")).strip(),
        "api_key": str(cfg.get("embedding", "api_key", "")).strip(),
        "timeout_s": int(cfg.get("embedding", "timeout_s", 30)),
        "endpoint": str(cfg.get("embedding", "endpoint", "/embeddings")).strip(),
        "input_type": str(cfg.get("embedding", "input_type", "document")).strip(),
        "device": str(cfg.get("embedding", "device", "cpu")).strip(),
        "batch_size": int(cfg.get("embedding", "batch_size", 64)),
        "max_workers": int(cfg.get("embedding", "max_workers", 4)),
        "max_retries": int(cfg.get("embedding", "max_retries", 2)),
        "retry_backoff_s": float(cfg.get("embedding", "retry_backoff_s", 0.5)),
        "stream_fetch_limit": int(cfg.get("embedding", "stream_fetch_limit", 0) or 0),
        "stream_commit_every_batches": int(cfg.get("embedding", "stream_commit_every_batches", 0) or 0),
        "stream_write_buffer_chunks": int(cfg.get("embedding", "stream_write_buffer_chunks", 0) or 0),
        "provider_max_concurrency": int(cfg.get("embedding", "provider_max_concurrency", 8)),
        "online_max_concurrency": int(cfg.get("embedding", "online_max_concurrency", 8)),
        "online_query_max_retries": int(cfg.get("embedding", "online_query_max_retries", 2)),
        "online_query_cache_size": int(cfg.get("embedding", "online_query_cache_size", 1024)),
        "online_query_cache_ttl_s": float(cfg.get("embedding", "online_query_cache_ttl_s", 300.0)),
        "fail_open_on_query": bool(cfg.get("embedding", "fail_open_on_query", True)),
        "retryable_status_codes": cfg.get_list("embedding", "retryable_status_codes"),
        "llama": cfg.get("embedding", "llama", {}) or {},
    }


def _vector_runtime_config(args: argparse.Namespace) -> dict[str, object]:
    cfg = _cfg(args)
    return {
        "backend": str(cfg.get("vector", "backend", "lancedb")).strip().lower(),
        "write_mode": str(cfg.get("vector", "write_mode", "dual")).strip().lower(),
        "lancedb": cfg.get("vector", "lancedb", {}) or {},
    }


def _configure_vector_delete_hook(store: SqliteStore, args: argparse.Namespace) -> None:
    _, write_stores = make_vector_stores(store, _vector_runtime_config(args))
    non_sqlite_stores = [vs for vs in write_stores if not isinstance(vs, SqliteVectorStore)]
    if not non_sqlite_stores:
        store.set_vector_delete_hook(None)
        return

    def _hook(chunk_ids: list[str]) -> None:
        for vector_store in non_sqlite_stores:
            vector_store.delete_by_chunk_ids(chunk_ids, embedding_version=None)

    store.set_vector_delete_hook(_hook)


def _chunk_runtime_config(args: argparse.Namespace) -> dict[str, object]:
    return {
        "target_tokens": int(_resolve(args, "target_tokens", "chunk", "target_tokens", 512)),
        "overlap_tokens": int(_resolve(args, "overlap_tokens", "chunk", "overlap_tokens", 48)),
        "include_leading_doc_comment": bool(
            _resolve(args, "include_leading_doc_comment", "chunk", "include_leading_doc_comment", True)
        ),
        "include_call_graph_context": bool(
            _resolve(args, "include_call_graph_context", "chunk", "include_call_graph_context", True)
        ),
        "call_context_max_each": int(
            _resolve(args, "call_context_max_each", "chunk", "call_context_max_each", 8)
        ),
        "leading_doc_max_lookback_lines": int(
            _resolve(args, "leading_doc_max_lookback_lines", "chunk", "leading_doc_max_lookback_lines", 120)
        ),
        "chunk_strategy": str(_resolve(args, "chunk_strategy", "chunk", "strategy", "ast")),
        "java_treesitter_fallback": bool(
            _resolve(args, "java_treesitter_fallback", "chunk", "java_treesitter_fallback", True)
        ),
        "java_container_policy": str(
            _resolve(args, "java_container_policy", "chunk", "java_container_policy", "leaf_preferred")
        ),
        "fallback_to_definition_span": bool(
            _resolve(args, "fallback_to_definition_span", "chunk", "fallback_to_definition_span", True)
        ),
        "ast_min_lines": int(
            _resolve(
                args,
                "ast_min_lines",
                "chunk",
                "ast_min_lines",
                _resolve(args, "scip_ast_min_lines", "chunk", "scip_ast_min_lines", 5),
            )
        ),
        "function_level_only": bool(
            _resolve(args, "function_level_only", "chunk", "function_level_only", True)
        ),
        "ast_parent_min_lines": int(
            _resolve(args, "ast_parent_min_lines", "chunk", "ast_parent_min_lines", 8)
        ),
        "ast_parent_min_tokens": int(
            _resolve(args, "ast_parent_min_tokens", "chunk", "ast_parent_min_tokens", 100)
        ),
        "sibling_merge_enabled": bool(
            _resolve(args, "sibling_merge_enabled", "chunk", "sibling_merge_enabled", True)
        ),
        "sibling_merge_small_max_tokens": int(
            _resolve(
                args,
                "sibling_merge_small_max_tokens",
                "chunk",
                "sibling_merge_small_max_tokens",
                100,
            )
        ),
        "sibling_merge_target_tokens": int(
            _resolve(
                args,
                "sibling_merge_target_tokens",
                "chunk",
                "sibling_merge_target_tokens",
                260,
            )
        ),
        "sibling_merge_max_gap_lines": int(
            _resolve(
                args,
                "sibling_merge_max_gap_lines",
                "chunk",
                "sibling_merge_max_gap_lines",
                3,
            )
        ),
    }


def cmd_ingest(args: argparse.Namespace) -> None:
    store = SqliteStore(args.db)
    try:
        _configure_vector_delete_hook(store, args)
        batch_size = int(_resolve(args, "batch_size", "ingest", "batch_size", 1000))
        stats = IngestionPipeline(store, batch_size=batch_size).run(
            input_path=args.input,
            repo=args.repo,
            commit=args.commit,
            index_version=_resolve(args, "index_version", "ingest", "index_version", "v1"),
            retries=int(_resolve(args, "retries", "ingest", "retries", 2)),
            source_root=_resolve(args, "source_root", "ingest", "source_root", ""),
        )
        _print_json(stats.__dict__)
    finally:
        store.close()


def cmd_index_java(args: argparse.Namespace) -> None:
    result = JavaIndexer(
        JavaIndexRequest(
            repo_root=args.repo_root,
            output_path=_resolve(args, "output", "java_index", "output", "index.scip"),
            scip_java_cmd=_resolve(args, "scip_java_cmd", "java_index", "scip_java_cmd", "scip-java"),
            build_tool=_resolve(args, "build_tool", "java_index", "build_tool", ""),
            targetroot=_resolve(args, "targetroot", "java_index", "targetroot", ""),
            cleanup=bool(_resolve(args, "cleanup", "java_index", "cleanup", True)),
            verbose=bool(_resolve(args, "verbose", "java_index", "verbose", False)),
            build_args=args.build_args or [],
            semanticdb_targetroot=_resolve(
                args,
                "semanticdb_targetroot",
                "java_index",
                "semanticdb_targetroot",
                "",
            ),
        )
    ).run()

    store = SqliteStore(args.db)
    try:
        _configure_vector_delete_hook(store, args)
        batch_size = int(_resolve(args, "batch_size", "ingest", "batch_size", 1000))
        stats = IngestionPipeline(store, batch_size=batch_size).run(
            input_path=result.output_path,
            repo=args.repo,
            commit=args.commit,
            index_version=_resolve(args, "index_version", "ingest", "index_version", "v1"),
            retries=int(_resolve(args, "retries", "ingest", "retries", 2)),
            source_root=args.repo_root,
        )
        _print_json(
            {
                "scip_java": {
                    "build_tool": result.build_tool,
                    "command": result.command,
                    "output_path": result.output_path,
                    "elapsed_ms": result.elapsed_ms,
                    "used_manual_fallback": result.used_manual_fallback,
                },
                "ingest": stats.__dict__,
            }
        )
    finally:
        store.close()


def cmd_purge_chunks(args: argparse.Namespace) -> None:
    store = SqliteStore(args.db)
    try:
        store.set_vector_delete_hook(None)
        cur = store.conn.execute("SELECT COUNT(*) AS c FROM chunks")
        total_chunks = int(cur.fetchone()["c"])
        cur = store.conn.execute(
            """
            SELECT COUNT(*) AS c FROM chunks c
            INNER JOIN documents d ON d.document_id = c.document_id
            WHERE d.repo = ? AND d.commit_hash = ?
            """,
            (args.repo, args.commit),
        )
        snap_chunks = int(cur.fetchone()["c"])

        # LanceDB 的 delete(谓词) 会大规模重写数据文件，百万行可跑十分钟以上。
        # 若当前库内 chunk 全部属于本次要清的快照，直接 drop_table；否则用前缀删（较慢）。
        force_lance_drop = bool(getattr(args, "lance_drop_table", False))
        _, write_stores = make_vector_stores(store, _vector_runtime_config(args))
        lance_action = "none"
        if snap_chunks > 0:
            for vs in write_stores:
                if not isinstance(vs, LanceDbVectorStore):
                    continue
                if force_lance_drop or snap_chunks == total_chunks:
                    vs.drop_table_if_exists()
                    lance_action = "drop_table"
                else:
                    vs.delete_by_chunk_id_prefix(f"{args.repo}:{args.commit}:")
                    lance_action = "delete_by_prefix"

        deleted = store.delete_chunks_for_repo_commit(
            args.repo, args.commit, invoke_vector_hook=False
        )
        _print_json(
            {
                "deleted_chunks": deleted,
                "repo": args.repo,
                "commit": args.commit,
                "chunks_total_before": total_chunks,
                "chunks_in_snapshot_before": snap_chunks,
                "lance_vectors": lance_action,
            }
        )
    finally:
        store.close()


def cmd_chunk(args: argparse.Namespace) -> None:
    store = SqliteStore(args.db)
    progress = _progress_printer("chunk")
    try:
        progress(f"start db={args.db} repo={args.repo} commit={args.commit}")
        pipeline = _make_embedding_pipeline(store, args, progress_callback=progress)
        chunk_cfg = _chunk_runtime_config(args)
        total = pipeline.build_chunks(
            repo=args.repo,
            commit=args.commit,
            embedding_version=_resolve_embedding_version(args),
            **chunk_cfg,
        )
        progress(f"done chunks={total}")
        _print_json({"chunks": total, "chunk_config": chunk_cfg})
    finally:
        close = getattr(progress, "close", None)
        if callable(close):
            close()
        store.close()


def cmd_embed(args: argparse.Namespace) -> None:
    store = SqliteStore(args.db)
    progress = _progress_printer("embed")
    try:
        progress(f"start db={args.db} version={_resolve_embedding_version(args)}")
        stats = _make_embedding_pipeline(store, args, progress_callback=progress).run(
            embedding_version=_resolve_embedding_version(args)
        )
        progress(
            "done "
            f"embedded_chunks={stats.embedded_chunks} "
            f"failed_batches={stats.failed_batches} "
            f"retried_batches={stats.retried_batches}"
        )
        _print_json({"embedding_version": _resolve_embedding_version(args), **stats.as_dict()})
    finally:
        close = getattr(progress, "close", None)
        if callable(close):
            close()
        store.close()


def cmd_query(args: argparse.Namespace) -> None:
    store = SqliteStore(args.db)
    try:
        service = _make_service(store, args)
        results = service.query(
            Query(
                text=args.query,
                mode=_resolve(args, "mode", "query", "mode", "hybrid"),
                top_k=int(_resolve(args, "top_k", "query", "top_k", 10)),
                blend_strategy=_resolve(args, "blend_strategy", "query", "blend_strategy", "linear"),
            ),
            include_code=bool(_resolve(args, "include_code", "query", "include_code", False)),
            max_code_chars=int(_resolve(args, "max_code_chars", "query", "max_code_chars", 1200)),
        )
        _print_json(_format_results(results))
    finally:
        store.close()


def cmd_query_structure(args: argparse.Namespace) -> None:
    store = SqliteStore(args.db)
    try:
        service = _make_service(store, args)
        query_factory = {
            "def-of": dsl_def_of,
            "refs-of": dsl_refs_of,
            "callers-of": dsl_callers_of,
            "callees-of": dsl_callees_of,
        }
        query = query_factory[args.op](args.symbol_id, top_k=int(args.top_k or 10))
        results = service.query(
            query,
            include_code=bool(args.include_code),
            max_code_chars=int(args.max_code_chars or 1200),
        )
        _print_json(_format_results(results))
    finally:
        store.close()


def cmd_eval(args: argparse.Namespace) -> None:
    store = SqliteStore(args.db)
    try:
        service = HybridRetrievalService(
            store,
            embedding_pipeline=_make_embedding_pipeline(store, args),
            default_embedding_version=_resolve_embedding_version(args),
        )
        evaluator = Evaluator(service)
        metrics = evaluator.run(
            dataset_path=args.dataset,
            mode=_resolve(args, "mode", "eval", "mode", "hybrid"),
            top_k=int(_resolve(args, "top_k", "eval", "top_k", 10)),
        )
        _print_json(Evaluator.format_metrics(metrics))
    finally:
        store.close()


def cmd_eval_graph(args: argparse.Namespace) -> None:
    store = SqliteStore(args.db)
    try:
        metrics = GraphEvaluator(store).run()
        _print_json(metrics.__dict__)
    finally:
        store.close()


def cmd_build_code_graph(args: argparse.Namespace) -> None:
    store = SqliteStore(args.db)
    try:
        stats = CodeGraphBuilder(store).build(repo=args.repo, commit=args.commit)
        _print_json(stats.__dict__)
    finally:
        store.close()


def cmd_build_intent_fn(args: argparse.Namespace) -> None:
    store = SqliteStore(args.db)
    try:
        stats = FunctionIntentBuilder(
            store,
            neighbor_top_k=int(_resolve(args, "neighbor_top_k", "intent", "neighbor_top_k", 5)),
            llm_model=_resolve(args, "llm_model", "intent", "model", ""),
            llm_api_base=_resolve(args, "llm_api_base", "intent", "api_base", ""),
            llm_api_key=_resolve(args, "llm_api_key", "intent", "api_key", ""),
            llm_timeout_s=int(_resolve(args, "llm_timeout_s", "intent", "timeout_s", 30)),
            llm_temperature=float(_resolve(args, "llm_temperature", "intent", "temperature", 0.0)),
            llm_max_tokens=int(_resolve(args, "llm_max_tokens", "intent", "max_tokens", 200)),
        ).build(
            model_version=_resolve(
                args,
                "intent_pipeline_version",
                "intent",
                "intent_pipeline_version",
                "llm-v1",
            ),
            prompt_version=_resolve(
                args,
                "intent_prompt_version",
                "intent",
                "intent_prompt_version",
                "p1",
            ),
        )
        _print_json(stats.__dict__)
    finally:
        store.close()


def cmd_build_intent_module(args: argparse.Namespace) -> None:
    store = SqliteStore(args.db)
    try:
        resolutions = None
        if args.resolutions:
            resolutions = [float(x.strip()) for x in args.resolutions.split(",") if x.strip()]
        if resolutions is None:
            cfg_res = _cfg(args).get_list("community", "resolutions")
            resolutions = [float(x) for x in cfg_res] if cfg_res else None
        stats = IntentCommunityBuilder(
            store,
            llm_model=_resolve(args, "llm_model", "intent", "model", ""),
            llm_api_base=_resolve(args, "llm_api_base", "intent", "api_base", ""),
            llm_api_key=_resolve(args, "llm_api_key", "intent", "api_key", ""),
            llm_timeout_s=int(_resolve(args, "llm_timeout_s", "intent", "timeout_s", 30)),
            llm_temperature=float(_resolve(args, "llm_temperature", "intent", "temperature", 0.0)),
            llm_max_tokens=int(_resolve(args, "llm_max_tokens", "intent", "max_tokens", 200)),
        ).build(
            alpha=float(_resolve(args, "alpha", "community", "alpha", 0.5)),
            beta=float(_resolve(args, "beta", "community", "beta", 0.4)),
            gamma=float(_resolve(args, "gamma", "community", "gamma", 0.1)),
            semantic_top_k=int(_resolve(args, "semantic_top_k", "community", "semantic_top_k", 20)),
            resolution=float(_resolve(args, "resolution", "community", "resolution", 1.0)),
            resolutions=resolutions,
            edge_min_weight=float(_resolve(args, "edge_min_weight", "community", "edge_min_weight", 0.05)),
            fallback_threshold=float(
                _resolve(args, "fallback_threshold", "community", "fallback_threshold", 0.35)
            ),
        )
        _print_json(stats.__dict__)
    finally:
        store.close()


def cmd_apply_isolated_policy(args: argparse.Namespace) -> None:
    store = SqliteStore(args.db)
    try:
        stats = IsolatedNodePolicy(
            store,
            force_threshold_default=float(
                _resolve(args, "force_threshold_default", "isolated_policy", "force_threshold_default", 0.55)
            ),
            force_threshold_uncertain=float(
                _resolve(args, "force_threshold_uncertain", "isolated_policy", "force_threshold_uncertain", 0.65)
            ),
            force_threshold_entrypoint=float(
                _resolve(args, "force_threshold_entrypoint", "isolated_policy", "force_threshold_entrypoint", 0.60)
            ),
        ).run()
        _print_json(stats.__dict__)
    finally:
        store.close()


def cmd_query_graph(args: argparse.Namespace) -> None:
    store = SqliteStore(args.db)
    try:
        service = GraphService(
            store,
            embedding_pipeline=_make_embedding_pipeline(store, args),
            default_embedding_version=_resolve_embedding_version(args),
        )
        graph_mode = _resolve(args, "graph_mode", "graph_query", "graph_mode", "code")
        if graph_mode == "code":
            hops = int(_resolve(args, "hops", "graph_query", "hops", 1))
            edge_type = _resolve(args, "edge_type", "graph_query", "edge_type", "calls")
            res = service.code_subgraph(seed_ids=args.seed_ids, hops=hops, edge_type=edge_type)
        elif graph_mode == "intent":
            res = service.intent_subgraph(community_ids=args.community_ids)
        else:
            res = service.explore(
                query=args.query,
                symbol=args.symbol,
                module_top_k=int(_resolve(args, "module_top_k", "graph_query", "module_top_k", 5)),
                function_top_k=int(_resolve(args, "function_top_k", "graph_query", "function_top_k", 8)),
                semantic_top_k=int(_resolve(args, "semantic_top_k", "graph_query", "semantic_top_k", 8)),
                seed_fusion=str(_resolve(args, "seed_fusion", "graph_query", "seed_fusion", "rrf")),
                module_seed_member_top_k=int(
                    _resolve(args, "module_seed_member_top_k", "graph_query", "module_seed_member_top_k", 3)
                ),
                explore_default_hops_module=int(
                    _resolve(args, "explore_default_hops_module", "graph_query", "explore_default_hops_module", 2)
                ),
                explore_default_hops_function=int(
                    _resolve(args, "explore_default_hops_function", "graph_query", "explore_default_hops_function", 1)
                ),
                min_seed_score=float(_resolve(args, "min_seed_score", "graph_query", "min_seed_score", 0.0)),
                edge_type=str(_resolve(args, "edge_type", "graph_query", "edge_type", "calls")),
                hops=int(args.hops) if args.hops is not None else None,
            )
        _print_json(res)
    finally:
        store.close()


def cmd_repair_calls(args: argparse.Namespace) -> None:
    store = SqliteStore(args.db)
    try:
        stats = CallsRepairer(store).run(
            top_k=int(_resolve(args, "top_k", "repair_calls", "top_k", 6)),
            sim_threshold=float(_resolve(args, "sim_threshold", "repair_calls", "sim_threshold", 0.58)),
            max_edges_per_node=int(
                _resolve(args, "max_edges_per_node", "repair_calls", "max_edges_per_node", 3)
            ),
            reclassify=bool(_resolve(args, "reclassify", "repair_calls", "reclassify", False)),
        )
        _print_json(stats.__dict__)
    finally:
        store.close()


def cmd_eval_baseline_compare(args: argparse.Namespace) -> None:
    """find_entity（索引）与源码 grep baseline 对比（同一 ``k``：返回条数 + recall@k）。"""
    k = int(args.top_k)
    grep_report = run_grep_baseline(args.repo_root, args.dataset, top_k=k)
    find_summary = None
    find_queries = None
    if getattr(args, "db", None):
        store = SqliteStore(args.db)
        try:
            er = run_entity_eval(store, args.dataset, top_k=k)
            find_summary = format_entity_eval_metrics(er.metrics)
            find_queries = er.queries
        finally:
            store.close()
    _print_json(
        grep_baseline_report_to_json(
            grep_report,
            find_entity_summary=find_summary,
            find_entity_queries=find_queries,
        )
    )


def cmd_eval_entity(args: argparse.Namespace) -> None:
    store = SqliteStore(args.db)
    try:
        report = run_entity_eval(
            store,
            dataset_path=args.dataset,
            top_k=int(args.top_k),
        )
        if getattr(args, "no_per_query", False):
            _print_json(format_entity_eval_metrics(report.metrics))
        else:
            _print_json(entity_eval_report_to_json(report))
    finally:
        store.close()


def cmd_eval_spring_jsonl(args: argparse.Namespace) -> None:
    from pathlib import Path as PathLib

    from .spring_jsonl_semantic_eval import run_spring_jsonl_semantic_eval

    out = run_spring_jsonl_semantic_eval(args)
    out["dataset_path"] = str(PathLib(args.jsonl).resolve())
    out_path = getattr(args, "output", None)
    if out_path:
        p = PathLib(out_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
        _print_json(
            {
                "written": str(p.resolve()),
                "metrics": out["metrics"],
                "samples": out["samples"],
            }
        )
        return
    if getattr(args, "no_per_query", False):
        _print_json({k: v for k, v in out.items() if k != "per_query"})
    else:
        _print_json(out)


def cmd_prune_spring_jsonl(args: argparse.Namespace) -> None:
    from pathlib import Path as PathLib

    from .spring_jsonl_semantic_eval import run_prune_spring_jsonl

    report = run_prune_spring_jsonl(args)
    out_path = getattr(args, "report_json", None)
    if out_path:
        p = PathLib(out_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        report = {**report, "report_written": str(p.resolve())}
    _print_json(report)


def cmd_eval_spring_semantic(args: argparse.Namespace) -> None:
    from pathlib import Path as PathLib

    from .spring_semantic_eval import run_spring_semantic_eval

    out = run_spring_semantic_eval(args)
    out["dataset_path"] = str(PathLib(args.dataset).resolve())
    out_path = getattr(args, "output", None)
    if out_path:
        p = PathLib(out_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
        _print_json(
            {
                "written": str(p.resolve()),
                "metrics": out["metrics"],
                "samples": out["samples"],
            }
        )
        return
    if getattr(args, "no_per_query", False):
        _print_json(
            {
                k: v
                for k, v in out.items()
                if k != "per_query"
            }
        )
    else:
        _print_json(out)


def cmd_find_entity(args: argparse.Namespace) -> None:
    store = SqliteStore(args.db)
    try:
        hits = find_entity(
            store,
            type=args.entity_type,
            name=args.name,
            match=args.match,
            package_contains=args.package_contains or "",
            limit=int(args.limit),
        )
        _print_json(
            {
                "entity_type": args.entity_type,
                "name": args.name,
                "match": args.match,
                "count": len(hits),
                "entities": [h.__dict__ for h in hits],
                "supported_types": list(entity_types()),
            }
        )
    finally:
        store.close()


def _chunk_section_from_args(args: argparse.Namespace) -> dict[str, object]:
    return _cfg(args).get_section("chunk")


def _query_section_from_args(args: argparse.Namespace) -> dict[str, object]:
    return _cfg(args).get_section("query")


def cmd_serve(args: argparse.Namespace) -> None:
    run_server(
        db_path=args.db,
        host=_resolve(args, "host", "server", "host", "0.0.0.0"),
        port=int(_resolve(args, "port", "server", "port", 8080)),
        embedding_runtime=_embedding_runtime_config(args),
        vector_runtime=_vector_runtime_config(args),
        chunk_runtime=_chunk_section_from_args(args),
        query_runtime=_query_section_from_args(args),
        default_embedding_version=_resolve_embedding_version(args),
    )


def cmd_mcp_streamable(args: argparse.Namespace) -> None:
    """云端 MCP：Streamable HTTP + Uvicorn；库路径仍以环境变量为准，可用 --db 写入本进程 environ。"""
    import os
    from pathlib import Path

    if getattr(args, "db", None):
        os.environ["HYBRID_DB"] = str(args.db)
    if getattr(args, "mcp_path", None):
        os.environ["HYBRID_MCP_PATH"] = str(args.mcp_path)
    if not (os.environ.get("HYBRID_CONFIG") or "").strip():
        cfg = getattr(args, "config_path_override", None) or getattr(
            args, "config_path", DEFAULT_CONFIG_PATH
        )
        if cfg:
            os.environ["HYBRID_CONFIG"] = str(Path(cfg).resolve())
    from .mcp_streamable_server import main as mcp_streamable_main

    mcp_streamable_main()


def _subparser_with_config(sub: Any, name: str, **kwargs: Any) -> argparse.ArgumentParser:
    """子解析器额外接受尾部 --config，解决「全局 --config 必须写在子命令前」的易错问题。"""
    p = sub.add_parser(name, **kwargs)
    p.add_argument(
        "--config",
        dest="config_path_override",
        default=None,
        metavar="PATH",
        help="配置文件路径（可写在子命令之后；若指定则覆盖命令行最前面的全局 --config）",
    )
    return p


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="SCIP hybrid retrieval CLI")
    parser.add_argument(
        "--config",
        dest="config_path",
        default=DEFAULT_CONFIG_PATH,
        help="配置文件路径（写在子命令之前；或在子命令后用该子命令自己的 --config，后者优先）",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    ingest = _subparser_with_config(sub, "ingest")
    ingest.add_argument("--repo", required=True)
    ingest.add_argument("--commit", required=True)
    ingest.add_argument("--input", required=True)
    ingest.add_argument("--db", required=True)
    ingest.add_argument("--index-version", default=None)
    ingest.add_argument("--batch-size", type=int, default=None)
    ingest.add_argument("--retries", type=int, default=None)
    ingest.add_argument("--source-root", default=None)
    ingest.set_defaults(func=cmd_ingest)

    index_java = _subparser_with_config(sub, "index-java")
    index_java.add_argument("--repo-root", required=True)
    index_java.add_argument("--repo", required=True)
    index_java.add_argument("--commit", required=True)
    index_java.add_argument("--db", required=True)
    index_java.add_argument("--output", default=None)
    index_java.add_argument("--index-version", default=None)
    index_java.add_argument("--batch-size", type=int, default=None)
    index_java.add_argument("--retries", type=int, default=None)
    index_java.add_argument("--scip-java-cmd", dest="scip_java_cmd", default=None)
    index_java.add_argument("--build-tool", choices=["maven", "gradle"], default=None)
    index_java.add_argument("--targetroot", default=None)
    index_java.add_argument("--semanticdb-targetroot", dest="semanticdb_targetroot", default=None)
    index_java.add_argument("--cleanup", action=argparse.BooleanOptionalAction, default=None)
    index_java.add_argument("--verbose", action=argparse.BooleanOptionalAction, default=None)
    index_java.add_argument("build_args", nargs=argparse.REMAINDER)
    index_java.set_defaults(func=cmd_index_java)

    purge_chunks = _subparser_with_config(
        sub,
        "purge-chunks",
        help="删除指定 repo+commit 下全部 chunk、SQLite embeddings 与 FTS；并按配置从 LanceDB 等向量库删行（不删 documents）",
    )
    purge_chunks.add_argument("--db", required=True)
    purge_chunks.add_argument("--repo", required=True)
    purge_chunks.add_argument("--commit", required=True)
    purge_chunks.add_argument(
        "--lance-drop-table",
        action="store_true",
        help="强制删除整个 Lance 向量表（最快）。库内另有其它快照 chunk 时不要使用，否则会丢掉其它快照的向量。",
    )
    purge_chunks.set_defaults(func=cmd_purge_chunks)

    chunk = _subparser_with_config(sub, "chunk")
    chunk.add_argument("--repo", required=True)
    chunk.add_argument("--commit", required=True)
    chunk.add_argument("--db", required=True)
    chunk.add_argument(
        "--embedding-version",
        default=None,
        dest="embedding_version",
        help="写入 chunks 的 embedding_version；默认取配置 embedding.version",
    )
    chunk.add_argument("--target-tokens", type=int, default=None)
    chunk.add_argument("--overlap-tokens", type=int, default=None)
    chunk.add_argument(
        "--chunk-strategy",
        choices=["ast", "scip_ast", "definition_span"],
        default=None,
        help="切分策略：ast=优先用 SCIP AST，Java 缺失时回退 tree-sitter；definition_span=最后兜底",
    )
    chunk.add_argument(
        "--java-treesitter-fallback",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Java 缺少 SCIP enclosing_range 时是否用 tree-sitter-java 做 AST 兜底",
    )
    chunk.add_argument(
        "--java-container-policy",
        choices=["all", "leaf_preferred"],
        default=None,
        help="Java AST chunk 中是否保留同时包裹成员声明的 type/container 块",
    )
    chunk.add_argument(
        "--fallback-to-definition-span",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="AST 路径无可用节点时是否回退 definition_span",
    )
    chunk.add_argument(
        "--ast-min-lines",
        type=int,
        default=None,
        help="AST 节点最小行数（过短节点会被跳过）",
    )
    chunk.add_argument("--scip-ast-min-lines", type=int, default=None, help=argparse.SUPPRESS)
    chunk.add_argument(
        "--include-leading-doc-comment",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="是否将定义前的文档注释/docstring纳入 chunk",
    )
    chunk.add_argument(
        "--include-call-graph-context",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="是否注入调用关系上下文（caller/callee）",
    )
    chunk.add_argument("--call-context-max-each", type=int, default=None)
    chunk.add_argument("--leading-doc-max-lookback-lines", type=int, default=None)
    chunk.add_argument(
        "--function-level-only",
        action=argparse.BooleanOptionalAction,
        default=None,
        dest="function_level_only",
        help="为 true 时仅方法/构造函数/函数独立成块，字段等归入父类型剩余行（ast_parent chunk）",
    )
    chunk.set_defaults(func=cmd_chunk)

    embed = _subparser_with_config(sub, "embed")
    embed.add_argument("--db", required=True)
    embed.add_argument(
        "--embedding-version",
        default=None,
        dest="embedding_version",
        help="写入 embeddings 表的版本标签；默认取配置 embedding.version（与 chunk 应一致）",
    )
    embed.set_defaults(func=cmd_embed)

    ee = _subparser_with_config(
        sub,
        "eval-entity",
        help="基于 find_entity 的准确率评测（数据集含 entity_query + relevant_ids）",
    )
    ee.add_argument("--db", required=True)
    ee.add_argument("--dataset", required=True, help="JSON：samples[].entity_query + relevant_ids")
    ee.add_argument("--top-k", type=int, default=10)
    ee.add_argument(
        "--no-per-query",
        action="store_true",
        help="仅输出 summary 指标，不打印每条 query 的 results",
    )
    ee.set_defaults(func=cmd_eval_entity)

    ess = _subparser_with_config(
        sub,
        "eval-spring-semantic",
        help="Spring 自然问句 + Java golden：纯 semantic 向量检索评测（golden 映射到 chunk_id）",
    )
    ess.add_argument("--db", required=True)
    ess.add_argument(
        "--dataset",
        required=True,
        help="JSON：samples[].query + samples[].golden（Java FQCN，可选 #method/arity）",
    )
    ess.add_argument("--repo", required=True)
    ess.add_argument("--commit", required=True)
    ess.add_argument("--top-k", type=int, default=10)
    ess.add_argument(
        "--embedding-version",
        default=None,
        dest="embedding_version",
        help="默认取配置 embedding.version，须与 chunk/embed 一致",
    )
    ess.add_argument(
        "--no-per-query",
        action="store_true",
        help="仅输出汇总 metrics，不输出每条 query",
    )
    ess.add_argument(
        "--output",
        "-o",
        default=None,
        metavar="PATH",
        help="将完整结果写入 JSON（UTF-8）：含每条 query、golden、retrieved 路径与 is_relevant 等，便于人工核对",
    )
    ess.set_defaults(func=cmd_eval_spring_semantic)

    esj = _subparser_with_config(
        sub,
        "eval-spring-jsonl",
        help="spring_semantic_queries.jsonl：query + ground_truth（gold_files | gold_symbols），semantic 评测",
    )
    esj.add_argument("--db", required=True)
    esj.add_argument(
        "--jsonl",
        required=True,
        help="JSONL：每行 {\"query\": \"...\", \"ground_truth\": {\"gold_files\": \"a | b\", \"gold_symbols\": \"...\"}}",
    )
    esj.add_argument("--repo", required=True)
    esj.add_argument("--commit", required=True)
    esj.add_argument("--top-k", type=int, default=10)
    esj.add_argument("--embedding-version", default=None, dest="embedding_version")
    esj.add_argument("--no-per-query", action="store_true")
    esj.add_argument(
        "--output",
        "-o",
        default=None,
        metavar="PATH",
        help="完整结果 JSON（含每条 query、ground_truth、retrieved）",
    )
    esj.set_defaults(func=cmd_eval_spring_jsonl)

    psj = _subparser_with_config(
        sub,
        "prune-spring-jsonl",
        help="按 db/repo/commit 解析 ground_truth，从 JSONL 中删除解析不到相关 chunk 的行（物理删行）",
    )
    psj.add_argument("--db", required=True)
    psj.add_argument(
        "--input-jsonl",
        required=True,
        help="输入 JSONL（与 eval-spring-jsonl 同格式）",
    )
    psj.add_argument(
        "--output-jsonl",
        required=True,
        help="输出 JSONL（仅保留至少解析到一个相关 chunk 的行）",
    )
    psj.add_argument("--repo", required=True)
    psj.add_argument("--commit", required=True)
    psj.add_argument(
        "--report-json",
        default=None,
        metavar="PATH",
        help="可选：将 kept/dropped/行号 写入 JSON 报告",
    )
    psj.set_defaults(func=cmd_prune_spring_jsonl)

    ebc = _subparser_with_config(
        sub,
        "eval-baseline-compare",
        help="grep/rg 源码 baseline 与 find_entity（需 --db）对比，见 gap 字段",
    )
    ebc.add_argument("--repo-root", required=True, help="Java 源码根目录（如 netty 克隆路径）")
    ebc.add_argument("--dataset", required=True, help="与 eval-entity 相同的 entity_eval JSON")
    ebc.add_argument("--db", default=None, help="若提供则计算 find_entity 的 summary 并输出 gap")
    ebc.add_argument("--top-k", type=int, default=10, help="find_entity 的 top_k（仅当 --db 时有效）")
    ebc.set_defaults(func=cmd_eval_baseline_compare)

    fe = _subparser_with_config(sub, "find-entity", help="按类型+名称查询符号（封装 symbols 表，非裸 SQL）")
    fe.add_argument("--db", required=True)
    fe.add_argument(
        "--type",
        dest="entity_type",
        required=True,
        help="逻辑类型: class, interface, enum, type(类/接口/枚举), method, field, constructor, variable, type_parameter, any",
    )
    fe.add_argument("--name", required=True, help="标识名（匹配 display_name 或 symbol_id 路径）")
    fe.add_argument(
        "--match",
        choices=["exact", "contains"],
        default="contains",
        help="exact=全等(忽略大小写); contains=子串",
    )
    fe.add_argument("--package-contains", default="", help="可选：package 或 symbol_id 需含该子串")
    fe.add_argument("--limit", type=int, default=50)
    fe.set_defaults(func=cmd_find_entity)

    query = _subparser_with_config(sub, "query")
    query.add_argument("--db", required=True)
    query.add_argument(
        "--embedding-version",
        default=None,
        dest="embedding_version",
        help="semantic/hybrid 使用的向量版本；默认取配置 embedding.version",
    )
    query.add_argument("--query", required=True)
    query.add_argument("--mode", choices=["structure", "semantic", "hybrid"], default=None)
    query.add_argument("--top-k", type=int, default=None)
    query.add_argument("--blend-strategy", choices=["linear", "rrf"], default=None)
    query.add_argument("--include-code", action=argparse.BooleanOptionalAction, default=None)
    query.add_argument("--max-code-chars", type=int, default=None)
    query.set_defaults(func=cmd_query)

    qs = _subparser_with_config(sub, "query-structure", help="按 symbol_id 执行结构化查询")
    qs.add_argument("--db", required=True)
    qs.add_argument("--op", required=True, choices=["def-of", "refs-of", "callers-of", "callees-of"])
    qs.add_argument("--symbol-id", required=True)
    qs.add_argument("--top-k", type=int, default=10)
    qs.add_argument("--include-code", action=argparse.BooleanOptionalAction, default=False)
    qs.add_argument("--max-code-chars", type=int, default=1200)
    qs.set_defaults(func=cmd_query_structure)

    ev = _subparser_with_config(sub, "eval")
    ev.add_argument("--db", required=True)
    ev.add_argument("--dataset", required=True)
    ev.add_argument("--mode", choices=["structure", "semantic", "hybrid"], default=None)
    ev.add_argument("--top-k", type=int, default=None)
    ev.set_defaults(func=cmd_eval)

    evg = _subparser_with_config(sub, "eval-graph")
    evg.add_argument("--db", required=True)
    evg.set_defaults(func=cmd_eval_graph)

    graph = _subparser_with_config(sub, "build-code-graph")
    graph.add_argument("--db", required=True)
    graph.add_argument("--repo", required=True)
    graph.add_argument("--commit", required=True)
    graph.set_defaults(func=cmd_build_code_graph)

    intent_fn = _subparser_with_config(sub, "build-intent-fn")
    intent_fn.add_argument("--db", required=True)
    intent_fn.add_argument("--intent-pipeline-version", dest="intent_pipeline_version", default=None)
    intent_fn.add_argument("--intent-prompt-version", dest="intent_prompt_version", default=None)
    intent_fn.add_argument("--llm-model", default=None)
    intent_fn.add_argument("--llm-api-base", default=None)
    intent_fn.add_argument("--llm-api-key", default=None)
    intent_fn.add_argument("--llm-timeout-s", type=int, default=None)
    intent_fn.add_argument("--llm-temperature", type=float, default=None)
    intent_fn.add_argument("--llm-max-tokens", type=int, default=None)
    intent_fn.add_argument("--neighbor-top-k", type=int, default=None)
    intent_fn.set_defaults(func=cmd_build_intent_fn)

    intent_module = _subparser_with_config(sub, "build-intent-module")
    intent_module.add_argument("--db", required=True)
    intent_module.add_argument("--alpha", type=float, default=None)
    intent_module.add_argument("--beta", type=float, default=None)
    intent_module.add_argument("--gamma", type=float, default=None)
    intent_module.add_argument("--semantic-top-k", type=int, default=None)
    intent_module.add_argument("--resolution", type=float, default=None)
    intent_module.add_argument("--resolutions", default=None)
    intent_module.add_argument("--edge-min-weight", type=float, default=None)
    intent_module.add_argument("--fallback-threshold", type=float, default=None)
    intent_module.add_argument("--llm-model", default=None)
    intent_module.add_argument("--llm-api-base", default=None)
    intent_module.add_argument("--llm-api-key", default=None)
    intent_module.add_argument("--llm-timeout-s", type=int, default=None)
    intent_module.add_argument("--llm-temperature", type=float, default=None)
    intent_module.add_argument("--llm-max-tokens", type=int, default=None)
    intent_module.set_defaults(func=cmd_build_intent_module)

    iso = _subparser_with_config(sub, "apply-isolated-policy")
    iso.add_argument("--db", required=True)
    iso.add_argument("--force-threshold-default", type=float, default=None)
    iso.add_argument("--force-threshold-uncertain", type=float, default=None)
    iso.add_argument("--force-threshold-entrypoint", type=float, default=None)
    iso.set_defaults(func=cmd_apply_isolated_policy)

    qg = _subparser_with_config(sub, "query-graph")
    qg.add_argument("--db", required=True)
    qg.add_argument("--graph-mode", choices=["code", "intent", "explore"], default=None)
    qg.add_argument("--seed-ids", nargs="*", default=[])
    qg.add_argument("--hops", type=int, default=None)
    qg.add_argument("--edge-type", default=None)
    qg.add_argument("--community-ids", nargs="*", default=[])
    qg.add_argument("--query")
    qg.add_argument("--symbol")
    qg.set_defaults(func=cmd_query_graph)

    repair = _subparser_with_config(sub, "repair-calls")
    repair.add_argument("--db", required=True)
    repair.add_argument("--top-k", type=int, default=None)
    repair.add_argument("--sim-threshold", type=float, default=None)
    repair.add_argument("--max-edges-per-node", type=int, default=None)
    repair.add_argument("--reclassify", action=argparse.BooleanOptionalAction, default=None)
    repair.set_defaults(func=cmd_repair_calls)

    serve = _subparser_with_config(sub, "serve")
    serve.add_argument("--db", required=True)
    serve.add_argument("--host", default=None)
    serve.add_argument("--port", type=int, default=None)
    serve.set_defaults(func=cmd_serve)

    mcp_st = _subparser_with_config(sub, "mcp-streamable", help="MCP Streamable HTTP（远程，无 stdio）")
    mcp_st.add_argument(
        "--db",
        default=None,
        help="覆盖环境变量 HYBRID_DB（SQLite 索引路径）",
    )
    mcp_st.add_argument(
        "--mcp-path",
        default=None,
        dest="mcp_path",
        metavar="PATH",
        help="覆盖环境变量 HYBRID_MCP_PATH（如按 repo+commit 区分为 /mcp/myrepo_abcd...）",
    )
    mcp_st.set_defaults(func=cmd_mcp_streamable)
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    cfg_path = getattr(args, "config_path_override", None) or getattr(args, "config_path", DEFAULT_CONFIG_PATH)
    args.app_config = AppConfig.load(cfg_path)
    args.func(args)


if __name__ == "__main__":
    main()
