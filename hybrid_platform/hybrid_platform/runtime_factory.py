"""从 AppConfig 构建 EmbeddingPipeline、向量存储与检索服务（供 CLI / HTTP / MCP 共用）。"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Dict, List, Optional

from .config import AppConfig
from .embedding import (
    DeterministicEmbedder,
    EmbeddingPipeline,
    HttpEmbeddingClient,
    LocalSentenceTransformerEmbedder,
    VoyageEmbeddingClient,
    make_chunk_token_count_fn,
    resolve_llama_init_kwargs,
)
from .graph_service import GraphService
from .llamaindex_embedder import LlamaIndexEmbedder
from .retrieval import HybridRetrievalService
from .storage import SqliteStore
from .vector_store import SqliteVectorStore, VectorStore, dedupe_vector_stores
from .vector_store_lancedb import LanceDbVectorStore


def embedding_runtime_dict_from_app_config(cfg: AppConfig) -> Dict[str, object]:
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


def vector_runtime_dict_from_app_config(cfg: AppConfig) -> Dict[str, object]:
    return {
        "backend": str(cfg.get("vector", "backend", "lancedb")).strip().lower(),
        "write_mode": str(cfg.get("vector", "write_mode", "dual")).strip().lower(),
        "lancedb": cfg.get("vector", "lancedb", {}) or {},
    }


def chunk_runtime_dict_from_app_config(cfg: AppConfig) -> Dict[str, object]:
    ch = cfg.get_section("chunk")
    return {
        "target_tokens": int(ch.get("target_tokens", 512)),
        "overlap_tokens": int(ch.get("overlap_tokens", 48)),
        "include_leading_doc_comment": bool(ch.get("include_leading_doc_comment", True)),
        "include_call_graph_context": bool(ch.get("include_call_graph_context", True)),
        "call_context_max_each": int(ch.get("call_context_max_each", 8)),
        "leading_doc_max_lookback_lines": int(ch.get("leading_doc_max_lookback_lines", 120)),
        "chunk_strategy": str(ch.get("strategy", "ast")),
        "java_treesitter_fallback": bool(ch.get("java_treesitter_fallback", True)),
        "java_container_policy": str(ch.get("java_container_policy", "leaf_preferred")),
        "fallback_to_definition_span": bool(ch.get("fallback_to_definition_span", True)),
        "ast_min_lines": int(ch.get("ast_min_lines", ch.get("scip_ast_min_lines", 5))),
        "function_level_only": bool(ch.get("function_level_only", True)),
        "ast_parent_min_lines": int(ch.get("ast_parent_min_lines", 8)),
        "ast_parent_min_tokens": int(ch.get("ast_parent_min_tokens", 100)),
        "sibling_merge_enabled": bool(ch.get("sibling_merge_enabled", True)),
        "sibling_merge_small_max_tokens": int(ch.get("sibling_merge_small_max_tokens", 100)),
        "sibling_merge_target_tokens": int(ch.get("sibling_merge_target_tokens", 260)),
        "sibling_merge_max_gap_lines": int(ch.get("sibling_merge_max_gap_lines", 3)),
    }


def default_embedding_version_from_app_config(cfg: AppConfig) -> str:
    return str(cfg.get("embedding", "version", "v1"))


def graph_query_dict_from_app_config(cfg: AppConfig) -> Dict[str, object]:
    g = cfg.get_section("graph_query")
    return {
        "graph_mode": str(g.get("graph_mode", "code")),
        "hops": int(g.get("hops", 1)),
        "edge_type": str(g.get("edge_type", "calls")),
        "module_top_k": int(g.get("module_top_k", 5)),
        "function_top_k": int(g.get("function_top_k", 8)),
        "semantic_top_k": int(g.get("semantic_top_k", 8)),
        "seed_fusion": str(g.get("seed_fusion", "rrf")),
        "module_seed_member_top_k": int(g.get("module_seed_member_top_k", 3)),
        "explore_default_hops_module": int(g.get("explore_default_hops_module", 2)),
        "explore_default_hops_function": int(g.get("explore_default_hops_function", 1)),
        "min_seed_score": float(g.get("min_seed_score", 0.0)),
    }


def make_vector_stores(
    store: SqliteStore,
    vector_runtime: Dict[str, object],
) -> tuple[VectorStore, List[VectorStore]]:
    backend = str(vector_runtime.get("backend", "lancedb")).strip().lower()
    write_mode = str(vector_runtime.get("write_mode", "dual")).strip().lower()
    lancedb_cfg = vector_runtime.get("lancedb", {}) or {}
    if not isinstance(lancedb_cfg, dict):
        raise ValueError("vector.lancedb must be a JSON object")
    sqlite_vs = SqliteVectorStore(store)
    lance_vs = None
    if backend == "lancedb" or write_mode in {"dual", "lancedb_only"}:
        uri = str(lancedb_cfg.get("uri", "")).strip() or str(
            Path(store.db_path).with_suffix(Path(store.db_path).suffix + ".lancedb")
        )
        table = str(lancedb_cfg.get("table", "chunk_vectors")).strip()
        metric = str(lancedb_cfg.get("metric", "cosine")).strip().lower()
        lance_vs = LanceDbVectorStore(uri=uri, table=table or "chunk_vectors", metric=metric or "cosine")
    if backend == "lancedb":
        if lance_vs is None:
            raise ValueError("vector.backend=lancedb but LanceDB is not configured")
        search_store = lance_vs
    else:
        search_store = sqlite_vs

    if write_mode == "sqlite_only":
        write_stores: List[VectorStore] = [sqlite_vs]
    elif write_mode == "lancedb_only":
        if lance_vs is None:
            raise ValueError("vector.write_mode=lancedb_only but LanceDB is not configured")
        write_stores = [lance_vs]
    elif write_mode == "dual":
        if lance_vs is None:
            raise ValueError("vector.write_mode=dual but LanceDB is not configured")
        write_stores = [sqlite_vs, lance_vs]
    else:
        raise ValueError("vector.write_mode must be sqlite_only | dual | lancedb_only")
    return search_store, dedupe_vector_stores(write_stores)


def make_embedding_pipeline(
    store: SqliteStore,
    embedding_runtime: Optional[Dict[str, object]],
    vector_runtime: Optional[Dict[str, object]],
    chunk_runtime: Optional[Dict[str, object]] = None,
    *,
    progress_callback: Optional[Callable[[str], None]] = None,
) -> EmbeddingPipeline:
    cfg = embedding_runtime or {}
    vector_cfg = vector_runtime or {}
    vector_search_store, vector_write_stores = make_vector_stores(store, vector_cfg)
    provider = str(cfg.get("provider", "deterministic")).strip().lower()
    model = str(cfg.get("model", "deterministic-hash-v1")).strip()
    chunk_cfg = chunk_runtime or {}
    if not isinstance(chunk_cfg, dict):
        chunk_cfg = {}
    tc_backend = str(chunk_cfg.get("token_counter", "auto") or "auto").strip().lower()
    tc_model = str(chunk_cfg.get("token_counter_model") or "").strip() or model
    chunk_token_count = make_chunk_token_count_fn(backend=tc_backend, model=tc_model or None)
    dim = int(cfg.get("dim", 128))
    api_base = str(cfg.get("api_base", "")).strip()
    api_key = str(cfg.get("api_key", "")).strip()
    timeout_s = int(cfg.get("timeout_s", 30))
    endpoint = str(cfg.get("endpoint", "/embeddings")).strip()
    batch_size = int(cfg.get("batch_size", 64))
    max_workers = int(cfg.get("max_workers", 4))
    max_retries = int(cfg.get("max_retries", 2))
    retry_backoff_s = float(cfg.get("retry_backoff_s", 0.5))
    provider_max_concurrency = int(cfg.get("provider_max_concurrency", 8))
    online_max_concurrency = int(cfg.get("online_max_concurrency", 8))
    online_query_max_retries = int(cfg.get("online_query_max_retries", max_retries))
    online_query_cache_size = int(cfg.get("online_query_cache_size", 1024))
    online_query_cache_ttl_s = float(cfg.get("online_query_cache_ttl_s", 300.0))
    fail_open_on_query = bool(cfg.get("fail_open_on_query", True))
    retryable_status_codes = cfg.get("retryable_status_codes", []) or []
    _sfl = cfg.get("stream_fetch_limit", 0)
    stream_fetch_limit = int(_sfl) if int(_sfl or 0) > 0 else None
    _sceb = cfg.get("stream_commit_every_batches", 0)
    stream_commit_every_batches = int(_sceb) if int(_sceb or 0) > 0 else None
    stream_write_buffer_chunks = int(cfg.get("stream_write_buffer_chunks", 0) or 0)

    common_kw = dict(
        batch_size=batch_size,
        max_workers=max_workers,
        max_retries=max_retries,
        retry_backoff_s=retry_backoff_s,
        vector_search_store=vector_search_store,
        vector_write_stores=vector_write_stores,
        provider_max_concurrency=provider_max_concurrency,
        online_max_concurrency=online_max_concurrency,
        online_query_max_retries=online_query_max_retries,
        online_query_cache_size=online_query_cache_size,
        online_query_cache_ttl_s=online_query_cache_ttl_s,
        fail_open_on_query=fail_open_on_query,
        retryable_status_codes=retryable_status_codes,
        progress_callback=progress_callback,
        stream_fetch_limit=stream_fetch_limit,
        stream_commit_every_batches=stream_commit_every_batches,
        stream_write_buffer_chunks=stream_write_buffer_chunks,
        chunk_token_count=chunk_token_count,
    )

    if provider == "http":
        if not model or not api_base:
            raise ValueError("embedding.provider=http requires embedding.model and embedding.api_base")
        return EmbeddingPipeline(
            store,
            embedder=HttpEmbeddingClient(
                model=model,
                api_base=api_base,
                api_key=api_key,
                timeout_s=timeout_s,
                endpoint=endpoint,
            ),
            **common_kw,
        )
    if provider == "voyage":
        if not model or not api_key:
            raise ValueError("embedding.provider=voyage requires embedding.model and embedding.api_key")
        return EmbeddingPipeline(
            store,
            embedder=VoyageEmbeddingClient(
                model=model,
                api_key=api_key,
                api_base=api_base or "https://api.voyageai.com",
                timeout_s=timeout_s,
                input_type=str(cfg.get("input_type", "document")),
            ),
            **common_kw,
        )
    if provider == "local":
        if not model:
            raise ValueError("embedding.provider=local requires embedding.model")
        return EmbeddingPipeline(
            store,
            embedder=LocalSentenceTransformerEmbedder(
                model=model,
                device=str(cfg.get("device", "cpu")),
            ),
            **common_kw,
        )
    if provider == "llamaindex":
        llama_cfg = cfg.get("llama", {}) or {}
        if not isinstance(llama_cfg, dict):
            raise ValueError("embedding.llama must be an object")
        class_path = str(llama_cfg.get("class_path", "llama_index.embeddings.voyageai.VoyageEmbedding")).strip()
        init_kwargs = llama_cfg.get("kwargs", {}) or {}
        if not isinstance(init_kwargs, dict):
            raise ValueError("embedding.llama.kwargs must be an object")
        common_arg_map = llama_cfg.get("common_arg_map", {}) or {}
        if not isinstance(common_arg_map, dict):
            raise ValueError("embedding.llama.common_arg_map must be an object")
        init_kwargs = resolve_llama_init_kwargs(
            dict(init_kwargs),
            common_arg_map,
            {
                "model": model,
                "api_base": api_base,
                "api_key": api_key,
                "timeout_s": timeout_s,
                "batch_size": batch_size,
                "dim": dim,
            },
        )
        return EmbeddingPipeline(
            store,
            embedder=LlamaIndexEmbedder(
                class_path=class_path,
                init_kwargs=dict(init_kwargs),
                query_method=str(llama_cfg.get("query_method", "query")).strip().lower(),
                document_method=str(llama_cfg.get("document_method", "text")).strip().lower(),
                allow_batch_fallback=bool(llama_cfg.get("allow_batch_fallback", True)),
                serialize_calls=bool(llama_cfg.get("serialize_calls", False)),
            ),
            **common_kw,
        )
    return EmbeddingPipeline(
        store,
        embedder=DeterministicEmbedder(dim=dim),
        **common_kw,
    )


def make_embedding_pipeline_from_app_config(
    store: SqliteStore,
    app_config: AppConfig,
    *,
    progress_callback: Optional[Callable[[str], None]] = None,
) -> EmbeddingPipeline:
    return make_embedding_pipeline(
        store,
        embedding_runtime_dict_from_app_config(app_config),
        vector_runtime_dict_from_app_config(app_config),
        chunk_runtime_dict_from_app_config(app_config),
        progress_callback=progress_callback,
    )


def make_hybrid_retrieval_service(
    store: SqliteStore,
    app_config: AppConfig,
    *,
    embedding_pipeline: Optional[EmbeddingPipeline] = None,
    progress_callback: Optional[Callable[[str], None]] = None,
) -> HybridRetrievalService:
    pipeline = embedding_pipeline or make_embedding_pipeline_from_app_config(
        store, app_config, progress_callback=progress_callback
    )
    qcfg = app_config.get_section("query")
    return HybridRetrievalService(
        store,
        embedding_pipeline=pipeline,
        default_embedding_version=default_embedding_version_from_app_config(app_config),
        test_code_depref_enabled=bool(qcfg.get("test_code_depref_enabled", True)),
        test_code_score_factor=float(qcfg.get("test_code_score_factor", 0.55)),
    )


def make_graph_service(
    store: SqliteStore,
    app_config: AppConfig,
    *,
    embedding_pipeline: Optional[EmbeddingPipeline] = None,
    progress_callback: Optional[Callable[[str], None]] = None,
) -> GraphService:
    pipeline = embedding_pipeline or make_embedding_pipeline_from_app_config(
        store, app_config, progress_callback=progress_callback
    )
    return GraphService(
        store,
        embedding_pipeline=pipeline,
        default_embedding_version=default_embedding_version_from_app_config(app_config),
    )


def format_query_results_for_json(results: list) -> list[dict]:
    return [
        {
            "id": r.result_id,
            "type": r.result_type,
            "score": round(float(r.score), 6),
            "explain": r.explain,
            "payload": r.payload,
        }
        for r in results
    ]
