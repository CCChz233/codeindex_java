"""面向 MCP 的只读工具实现：返回 JSON 字符串（ok / results / error）。"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import List, Optional

from .config import AppConfig
from .dsl import Query, callees_of, callers_of, def_of, refs_of
from .entity_query import entity_types, find_entity, normalize_entity_type
from .graph_service import GraphService
from .mcp_errors import (
    INPUT_VALIDATION,
    UNSUPPORTED_OPERATION,
    exception_to_mcp_error,
    mcp_error,
)
from .retrieval import HybridRetrievalService
from .runtime_factory import (
    default_embedding_version_from_app_config,
    format_query_results_for_json,
    graph_query_dict_from_app_config,
    make_embedding_pipeline_from_app_config,
    make_graph_service,
    make_hybrid_retrieval_service,
)
from .storage import SqliteStore


class CodeindexMcpRuntime:
    """进程内会话：共享 SqliteStore 与单一 EmbeddingPipeline（检索 + 图）。"""

    def __init__(self, db_path: str, config_path: str) -> None:
        self.db_path = str(Path(db_path).resolve())
        self.config_path = str(Path(config_path).resolve())
        self.app_config = AppConfig.load(self.config_path)
        self.store = SqliteStore(self.db_path)
        self._lock = threading.Lock()
        self._pipeline = None
        self._retrieval: HybridRetrievalService | None = None
        self._graph: GraphService | None = None

    def close(self) -> None:
        with self._lock:
            self.store.close()

    def _embedding_pipeline(self):
        if self._pipeline is None:
            self._pipeline = make_embedding_pipeline_from_app_config(self.store, self.app_config)
        return self._pipeline

    def retrieval_service(self) -> HybridRetrievalService:
        if self._retrieval is None:
            self._retrieval = make_hybrid_retrieval_service(
                self.store,
                self.app_config,
                embedding_pipeline=self._embedding_pipeline(),
            )
        return self._retrieval

    def graph_service(self) -> GraphService:
        if self._graph is None:
            self._graph = make_graph_service(
                self.store,
                self.app_config,
                embedding_pipeline=self._embedding_pipeline(),
            )
        return self._graph

    def default_embedding_version(self) -> str:
        return default_embedding_version_from_app_config(self.app_config)

    _SEMANTIC_QUERY_MODES = frozenset({"hybrid", "semantic"})

    def handle_semantic_query(
        self,
        query: str,
        mode: str = "hybrid",
        top_k: int = 10,
        blend_strategy: str = "linear",
        include_code: bool = False,
        max_code_chars: int = 1200,
        embedding_version: Optional[str] = None,
    ) -> str:
        tool = "semantic_query"
        try:
            q = (query or "").strip()
            if not q:
                return json.dumps(
                    {
                        "ok": False,
                        "tool": tool,
                        "error": mcp_error(
                            INPUT_VALIDATION,
                            "query must not be empty or whitespace-only",
                            suggested_next_steps=[
                                "Provide a non-empty natural-language or symbol-related query.",
                                "If you know an exact type name, use find_symbol then symbol_graph.",
                            ],
                        ),
                    },
                    ensure_ascii=False,
                )
            mode_norm = (mode or "hybrid").strip().lower()
            if mode_norm not in self._SEMANTIC_QUERY_MODES:
                hint = (
                    'mode "structure" is not supported on semantic_query; use find_symbol for entity type + name lookup.'
                    if mode_norm == "structure"
                    else f'unsupported mode {mode!r}; use "hybrid" or "semantic".'
                )
                return json.dumps(
                    {
                        "ok": False,
                        "tool": tool,
                        "error": mcp_error(
                            INPUT_VALIDATION,
                            hint,
                            suggested_next_steps=[
                                "Use mode hybrid (default) or semantic.",
                                "For symbol lookup by type and name, call find_symbol.",
                            ],
                        ),
                    },
                    ensure_ascii=False,
                )
            ver = embedding_version or self.default_embedding_version()
            with self._lock:
                svc = self.retrieval_service()
                results = svc.query(
                    Query(text=q, mode=mode_norm, top_k=top_k, blend_strategy=blend_strategy),
                    embedding_version=ver,
                    include_code=include_code,
                    max_code_chars=max_code_chars,
                )
            return json.dumps(
                {
                    "ok": True,
                    "tool": tool,
                    "results": format_query_results_for_json(results),
                },
                ensure_ascii=False,
            )
        except Exception as exc:
            return json.dumps(
                {
                    "ok": False,
                    "tool": tool,
                    "error": exception_to_mcp_error(exc, self.db_path),
                },
                ensure_ascii=False,
            )

    def handle_find_symbol(
        self,
        entity_type: str,
        name: str,
        match: str = "contains",
        package_contains: str = "",
        limit: int = 50,
    ) -> str:
        tool = "find_symbol"
        try:
            normalize_entity_type(entity_type)
            nm = (name or "").strip()
            if not nm:
                return json.dumps(
                    {
                        "ok": False,
                        "tool": tool,
                        "error": mcp_error(
                            INPUT_VALIDATION,
                            "name must not be empty or whitespace-only",
                            suggested_next_steps=["Provide the symbol display name or short name to look up."],
                        ),
                    },
                    ensure_ascii=False,
                )
            with self._lock:
                hits = find_entity(
                    self.store,
                    type=entity_type,
                    name=name,
                    match=match,
                    package_contains=package_contains or "",
                    limit=int(limit),
                )
            entities = [
                {
                    "symbol_id": h.symbol_id,
                    "display_name": h.display_name,
                    "kind": h.kind,
                    "package": h.package,
                    "language": h.language,
                    "enclosing_symbol": h.enclosing_symbol,
                }
                for h in hits
            ]
            return json.dumps(
                {
                    "ok": True,
                    "tool": tool,
                    "entity_type": entity_type,
                    "name": name,
                    "match": match,
                    "count": len(entities),
                    "entities": entities,
                    "supported_types": list(entity_types()),
                },
                ensure_ascii=False,
            )
        except ValueError as exc:
            return json.dumps(
                {
                    "ok": False,
                    "tool": tool,
                    "error": mcp_error(
                        INPUT_VALIDATION,
                        str(exc),
                        suggested_next_steps=[
                            "Use an entity_type from supported_types in a successful find_symbol response or the tool documentation.",
                            "For Java interfaces use interface, not class; if unsure use type or any.",
                        ],
                    ),
                },
                ensure_ascii=False,
            )
        except Exception as exc:
            return json.dumps(
                {
                    "ok": False,
                    "tool": tool,
                    "error": exception_to_mcp_error(exc, self.db_path),
                },
                ensure_ascii=False,
            )

    def handle_symbol_graph(
        self,
        op: str,
        symbol_id: str,
        top_k: int = 10,
        include_code: bool = False,
        max_code_chars: int = 1200,
        embedding_version: Optional[str] = None,
    ) -> str:
        tool = "symbol_graph"
        factories = {
            "def_of": def_of,
            "refs_of": refs_of,
            "callers_of": callers_of,
            "callees_of": callees_of,
        }
        try:
            op_n = (op or "").strip()
            if op_n not in factories:
                valid = sorted(factories.keys())
                return json.dumps(
                    {
                        "ok": False,
                        "tool": tool,
                        "error": mcp_error(
                            UNSUPPORTED_OPERATION,
                            f"op must be one of {valid}; got {op_n!r}",
                            suggested_next_steps=[
                                "def_of: definition site; refs_of: references; callers_of / callees_of: call edges.",
                                "symbol_id must be the full id from find_symbol or query results.",
                            ],
                        ),
                    },
                    ensure_ascii=False,
                )
            sid = (symbol_id or "").strip()
            if not sid:
                return json.dumps(
                    {
                        "ok": False,
                        "tool": tool,
                        "error": mcp_error(
                            INPUT_VALIDATION,
                            "symbol_id must not be empty",
                            suggested_next_steps=["Use find_symbol or semantic_query to obtain a full symbol_id first."],
                        ),
                    },
                    ensure_ascii=False,
                )
            ver = embedding_version or self.default_embedding_version()
            q = factories[op_n](sid, top_k=int(top_k))
            with self._lock:
                svc = self.retrieval_service()
                results = svc.query(
                    q,
                    embedding_version=ver,
                    include_code=include_code,
                    max_code_chars=max_code_chars,
                )
            return json.dumps(
                {
                    "ok": True,
                    "tool": tool,
                    "op": op_n,
                    "symbol_id": sid,
                    "results": format_query_results_for_json(results),
                },
                ensure_ascii=False,
            )
        except Exception as exc:
            return json.dumps(
                {
                    "ok": False,
                    "tool": tool,
                    "error": exception_to_mcp_error(exc, self.db_path),
                },
                ensure_ascii=False,
            )

    def handle_code_graph_explore(
        self,
        graph_mode: str,
        seed_ids: Optional[List[str]] = None,
        hops: Optional[int] = None,
        edge_type: str = "calls",
        community_ids: Optional[List[str]] = None,
        query: Optional[str] = None,
        symbol: Optional[str] = None,
        module_top_k: Optional[int] = None,
        function_top_k: Optional[int] = None,
        semantic_top_k: Optional[int] = None,
        seed_fusion: Optional[str] = None,
        module_seed_member_top_k: Optional[int] = None,
        explore_default_hops_module: Optional[int] = None,
        explore_default_hops_function: Optional[int] = None,
        min_seed_score: Optional[float] = None,
        embedding_version: Optional[str] = None,
    ) -> str:
        tool = "code_graph_explore"
        gdefaults = graph_query_dict_from_app_config(self.app_config)
        try:
            mode = (graph_mode or "").strip().lower()
            if mode not in {"code", "intent", "explore"}:
                return json.dumps(
                    {
                        "ok": False,
                        "tool": tool,
                        "error": mcp_error(
                            UNSUPPORTED_OPERATION,
                            "graph_mode must be code | intent | explore",
                            suggested_next_steps=[
                                "code: provide seed_ids (e.g. method:<symbol_id>).",
                                "intent: provide community_ids.",
                                "explore: provide query and/or symbol.",
                            ],
                        ),
                    },
                    ensure_ascii=False,
                )
            ver = embedding_version or self.default_embedding_version()
            with self._lock:
                gs = self.graph_service()
                if mode == "code":
                    res = gs.code_subgraph(
                        seed_ids=list(seed_ids or []),
                        hops=int(hops if hops is not None else gdefaults["hops"]),
                        edge_type=str(edge_type or gdefaults["edge_type"]),
                    )
                elif mode == "intent":
                    res = gs.intent_subgraph(community_ids=list(community_ids or []))
                else:
                    res = gs.explore(
                        query=query,
                        symbol=symbol,
                        module_top_k=int(module_top_k if module_top_k is not None else gdefaults["module_top_k"]),
                        function_top_k=int(
                            function_top_k if function_top_k is not None else gdefaults["function_top_k"]
                        ),
                        semantic_top_k=int(
                            semantic_top_k if semantic_top_k is not None else gdefaults["semantic_top_k"]
                        ),
                        seed_fusion=str(seed_fusion or gdefaults["seed_fusion"]),
                        module_seed_member_top_k=int(
                            module_seed_member_top_k
                            if module_seed_member_top_k is not None
                            else gdefaults["module_seed_member_top_k"]
                        ),
                        explore_default_hops_module=int(
                            explore_default_hops_module
                            if explore_default_hops_module is not None
                            else gdefaults["explore_default_hops_module"]
                        ),
                        explore_default_hops_function=int(
                            explore_default_hops_function
                            if explore_default_hops_function is not None
                            else gdefaults["explore_default_hops_function"]
                        ),
                        min_seed_score=float(
                            min_seed_score if min_seed_score is not None else gdefaults["min_seed_score"]
                        ),
                        edge_type=str(edge_type or gdefaults["edge_type"]),
                        hops=int(hops) if hops is not None else None,
                        embedding_version=ver,
                    )
            return json.dumps({"ok": True, "tool": tool, "graph_mode": mode, "data": res}, ensure_ascii=False)
        except Exception as exc:
            return json.dumps(
                {
                    "ok": False,
                    "tool": tool,
                    "error": exception_to_mcp_error(exc, self.db_path),
                },
                ensure_ascii=False,
            )
