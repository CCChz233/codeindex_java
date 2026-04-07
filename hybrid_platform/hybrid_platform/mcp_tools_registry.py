"""向 FastMCP 实例注册 codeindex 三只读工具（stdio / Streamable HTTP 复用）。"""

from __future__ import annotations

from typing import Callable, Union

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

from .agent_mcp_handlers import CodeindexMcpRuntime
from .mcp_env_runtime import get_mcp_runtime
from .mcp_errors import tool_result_config_error

_READ_ONLY = ToolAnnotations(readOnlyHint=True)

# MCP 暴露给 Agent 的参数保持最小；下列由服务端固定，与 HTTP POST /query 等可配置面区分。
_AGENT_SEMANTIC_MODE = "semantic"  # MCP 仅暴露 query；HTTP /query 仍可选 hybrid/structure
_AGENT_SEMANTIC_TOP_K = 10
_AGENT_SEMANTIC_BLEND = "linear"
_AGENT_INCLUDE_CODE = False
_AGENT_MAX_CODE_CHARS = 1200
_AGENT_SYMBOL_TOP_K = 10

RuntimeOrErrorStr = Union[CodeindexMcpRuntime, str]


def _default_rt_or_error() -> RuntimeOrErrorStr:
    r = get_mcp_runtime()
    if r is None:
        return tool_result_config_error(
            "Set HYBRID_DB to an existing SQLite index file path; optionally set HYBRID_CONFIG."
        )
    return r


def register_codeindex_tools(
    mcp: FastMCP,
    *,
    rt_or_error: Callable[[], RuntimeOrErrorStr] | None = None,
) -> None:
    """注册 semantic_query / find_symbol / symbol_graph（不含 code_graph_explore；多跳图请用 HTTP）。"""

    def _rt() -> RuntimeOrErrorStr:
        return (rt_or_error or _default_rt_or_error)()

    @mcp.tool(
        name="semantic_query",
        title="Semantic search over code index",
        description=(
            "Semantic retrieval over the ingested code index: returns relevant code chunks and symbols. "
            "Arguments: only `query` — non-empty natural language in English (questions, behavior descriptions, or search phrases work best). "
            "For exact type/method names or to obtain `symbol_id` for symbol_graph, use find_symbol instead. "
            "Read-only; may call external embedding APIs when configured. "
            "Returns a JSON string: ok=true includes results[{id,type,score,explain,payload}]; failures include an error object."
        ),
        annotations=_READ_ONLY,
    )
    def semantic_query(query: str) -> str:
        r = _rt()
        if isinstance(r, str):
            return r
        return r.handle_semantic_query(
            query=query,
            mode=_AGENT_SEMANTIC_MODE,
            top_k=_AGENT_SEMANTIC_TOP_K,
            blend_strategy=_AGENT_SEMANTIC_BLEND,
            include_code=_AGENT_INCLUDE_CODE,
            max_code_chars=_AGENT_MAX_CODE_CHARS,
            embedding_version=None,
        )

    @mcp.tool(
        name="find_symbol",
        title="Resolve symbol_id by entity type and name",
        description=(
            "Look up symbols in the symbols table by entity type and name; returns full symbol_id and display metadata. "
            "Use when the user gives a class/interface/method name and you need symbol_id for symbol_graph. "
            "For vague natural language, use semantic_query instead. For Java interfaces use entity_type=interface, not class. "
            "Read-only SQLite; no side effects beyond the query. "
            "Parameters: entity_type such as class, interface, method, type, any; match is exact | contains. "
            "Returns a JSON string: ok=true includes entities[], supported_types; count may be 0 (not an error)."
        ),
        annotations=_READ_ONLY,
    )
    def find_symbol(
        entity_type: str,
        name: str,
        match: str = "contains",
        package_contains: str = "",
        limit: int = 50,
    ) -> str:
        r = _rt()
        if isinstance(r, str):
            return r
        return r.handle_find_symbol(
            entity_type=entity_type,
            name=name,
            match=match,
            package_contains=package_contains,
            limit=limit,
        )

    @mcp.tool(
        name="symbol_graph",
        title="Symbol graph: definition, references, call edges",
        description=(
            "Run def_of, refs_of, callers_of, or callees_of for one symbol_id; relationships match the index. "
            "Use when you already have symbol_id and need definition site, referrers, or call direction. "
            "If symbol_id is missing, call find_symbol first. "
            "Read-only; server applies fixed top_k and snippet defaults (not configurable via this tool). "
            "Arguments: `op` and `symbol_id` only. "
            "Parameter op must be one of: def_of, refs_of, callers_of, callees_of. "
            "Returns a JSON string: ok=true includes op, symbol_id, results (same item shape as semantic_query)."
        ),
        annotations=_READ_ONLY,
    )
    def symbol_graph(op: str, symbol_id: str) -> str:
        r = _rt()
        if isinstance(r, str):
            return r
        return r.handle_symbol_graph(
            op=op,
            symbol_id=symbol_id,
            top_k=_AGENT_SYMBOL_TOP_K,
            include_code=_AGENT_INCLUDE_CODE,
            max_code_chars=_AGENT_MAX_CODE_CHARS,
            embedding_version=None,
        )
