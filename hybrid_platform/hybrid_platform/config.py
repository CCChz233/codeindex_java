from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List


def _deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    result = dict(base)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(result.get(k), dict):
            result[k] = _deep_merge(result[k], v)
        else:
            result[k] = v
    return result


@dataclass
class AppConfig:
    values: Dict[str, Any] = field(default_factory=dict)

    @staticmethod
    def defaults() -> Dict[str, Any]:
        return {
            "ingest": {
                "index_version": "v1",
                "batch_size": 1000,
                "retries": 2,
                "source_root": "",
            },
            "java_index": {
                "scip_java_cmd": "scip-java",
                "build_tool": "",
                "output": "index.scip",
                "targetroot": "",
                "cleanup": True,
                "verbose": False,
                "semanticdb_targetroot": "",
                "fallback_mode": "syntax",
            },
            "chunk": {
                "target_tokens": 512,
                "overlap_tokens": 48,
                "token_counter": "auto",
                "token_counter_model": "",
                "strategy": "ast",
                "java_treesitter_fallback": True,
                "java_container_policy": "leaf_preferred",
                "fallback_to_definition_span": True,
                "ast_min_lines": 5,
                "include_leading_doc_comment": True,
                "include_call_graph_context": True,
                "call_context_max_each": 8,
                "leading_doc_max_lookback_lines": 120,
                "function_level_only": True,
                "ast_parent_min_lines": 8,
                "ast_parent_min_tokens": 100,
                "sibling_merge_enabled": True,
                "sibling_merge_small_max_tokens": 100,
                "sibling_merge_target_tokens": 260,
                "sibling_merge_max_gap_lines": 3,
            },
            "embed": {},
            "embedding": {
                "version": "v1",
                "provider": "llamaindex",
                "model": "voyage-code-3",
                "dim": 256,
                "api_base": "",
                "api_key": "",
                "timeout_s": 30,
                "endpoint": "/embeddings",
                "batch_size": 64,
                "max_workers": 4,
                "max_retries": 2,
                "retry_backoff_s": 0.5,
                "stream_fetch_limit": 0,
                "stream_commit_every_batches": 2000,
                "stream_write_buffer_chunks": 0,
                "provider_max_concurrency": 8,
                "online_max_concurrency": 8,
                "online_query_max_retries": 2,
                "online_query_cache_size": 1024,
                "online_query_cache_ttl_s": 300.0,
                "fail_open_on_query": True,
                "retryable_status_codes": [],
                "input_type": "document",
                "device": "cpu",
                "llama": {
                    "class_path": "llama_index.embeddings.voyageai.VoyageEmbedding",
                    "kwargs": {
                        "model_name": "",
                        "voyage_api_key": "",
                        "embed_batch_size": 0,
                        "output_dimension": 0,
                    },
                    "common_arg_map": {
                        "model": "model_name",
                        "api_key": "voyage_api_key",
                        "batch_size": "embed_batch_size",
                        "dim": "output_dimension",
                        "api_base": "",
                    },
                    "query_method": "query",
                    "document_method": "text",
                    "allow_batch_fallback": True,
                    "serialize_calls": False,
                },
            },
            "vector": {
                "backend": "lancedb",
                "write_mode": "dual",
                "lancedb": {
                    "uri": "",
                    "table": "chunk_vectors",
                    "metric": "cosine",
                },
            },
            "query": {
                "mode": "hybrid",
                "top_k": 10,
                "blend_strategy": "linear",
                "include_code": False,
                "max_code_chars": 1200,
                "test_code_depref_enabled": True,
                "test_code_score_factor": 0.55,
            },
            "eval": {"mode": "hybrid", "top_k": 10},
            "server": {"host": "0.0.0.0", "port": 8080},
            "admin_index": {
                "max_concurrent_jobs": 2,
                "max_queue_size": 16,
            },
            "intent": {
                "intent_pipeline_version": "llm-v1",
                "intent_prompt_version": "p1",
                "neighbor_top_k": 5,
                "model": "",
                "api_base": "",
                "api_key": "",
                "timeout_s": 30,
                "temperature": 0.0,
                "max_tokens": 200,
            },
            "community": {
                "alpha": 0.5,
                "beta": 0.4,
                "gamma": 0.1,
                "semantic_top_k": 20,
                "resolution": 1.0,
                "resolutions": [],
                "edge_min_weight": 0.05,
                "fallback_threshold": 0.35,
            },
            "isolated_policy": {
                "force_threshold_default": 0.55,
                "force_threshold_uncertain": 0.65,
                "force_threshold_entrypoint": 0.60,
            },
            "repair_calls": {
                "top_k": 6,
                "sim_threshold": 0.58,
                "max_edges_per_node": 3,
                "reclassify": False,
            },
            "graph_query": {
                "graph_mode": "code",
                "hops": 1,
                "edge_type": "calls",
                "module_top_k": 5,
                "function_top_k": 8,
                "semantic_top_k": 8,
                "seed_fusion": "rrf",
                "module_seed_member_top_k": 3,
                "explore_default_hops_module": 2,
                "explore_default_hops_function": 1,
                "min_seed_score": 0.0,
            },
        }

    @classmethod
    def load(cls, path: str | None = None) -> "AppConfig":
        defaults = cls.defaults()
        if not path:
            return cls(values=defaults)
        p = Path(path)
        if not p.exists():
            return cls(values=defaults)
        data = json.loads(p.read_text(encoding="utf-8"))
        merged = _deep_merge(defaults, data)
        return cls(values=merged)

    @classmethod
    def merge_with_defaults(cls, data: Dict[str, Any]) -> "AppConfig":
        """将内联 JSON 对象与默认配置深度合并（供 HTTP 管理接口传入 config 使用）。"""
        merged = _deep_merge(cls.defaults(), data if isinstance(data, dict) else {})
        return cls(values=merged)

    def get(self, section: str, key: str, fallback: Any = None) -> Any:
        return self.values.get(section, {}).get(key, fallback)

    def get_section(self, section: str) -> Dict[str, Any]:
        """返回配置中某一整段（如 chunk、embedding）；缺失或非 object 时返回空 dict。"""
        raw = self.values.get(section, {})
        return dict(raw) if isinstance(raw, dict) else {}

    def get_list(self, section: str, key: str) -> List[Any]:
        raw = self.values.get(section, {}).get(key, [])
        if isinstance(raw, list):
            return raw
        return []
