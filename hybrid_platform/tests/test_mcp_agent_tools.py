"""
Simulate agent MCP calls: each case is intent → tool args → expected output.

Golden data from ``examples/sample.scip.ndjson`` (ingested as demo/repo @ abc123):
  - main calls add; parse_options lives in options.cc.
"""

from __future__ import annotations

import json

import pytest

from hybrid_platform.agent_mcp_handlers import CodeindexMcpRuntime

# Symbols/paths aligned with sample.scip.ndjson (find_symbol then follow-up should match)
SYMBOL_ADD = "scip-cpp demo add()."
SYMBOL_MAIN = "scip-cpp demo main()."
SYMBOL_PARSE_OPTIONS = "scip-cpp demo parse_options()."
# code_nodes.node_id — code_subgraph seed must be node_id, not symbol_id alone
NODE_ADD = "method:" + SYMBOL_ADD
NODE_MAIN = "method:" + SYMBOL_MAIN
PATH_MAIN_CC = "src/main.cc"
PATH_OPTIONS_CC = "src/options.cc"


@pytest.fixture
def runtime(mcp_fixture_db: tuple[str, str]) -> CodeindexMcpRuntime:
    db_path, cfg_path = mcp_fixture_db
    return CodeindexMcpRuntime(db_path, cfg_path)


def _j(s: str) -> dict:
    return json.loads(s)


def _collect_query_result_ids(results: list) -> set[str]:
    return {str(r["id"]) for r in results}


def _payload_paths(results: list) -> list[str]:
    out: list[str] = []
    for r in results:
        pl = r.get("payload") or {}
        if isinstance(pl, dict):
            p = pl.get("path")
            if isinstance(p, str):
                out.append(p)
    return out


class TestAgentFindSymbol:
    """Resolve symbols by type+name (feeds symbol_graph / graph queries)."""

    def test_intent_find_add_exact_entity(self, runtime: CodeindexMcpRuntime) -> None:
        """User wants symbol named add → find_symbol(any, add, exact) → must include SCIP symbol id."""
        d = _j(runtime.handle_find_symbol("any", "add", match="exact", limit=20))
        assert d["ok"] is True, d
        assert d["tool"] == "find_symbol"
        ids = {e["symbol_id"] for e in d["entities"]}
        assert SYMBOL_ADD in ids, f"expected {SYMBOL_ADD!r} in entities={d['entities']}"

    def test_intent_find_main_then_display_name(self, runtime: CodeindexMcpRuntime) -> None:
        """User wants main → contains → exactly one demo main symbol."""
        d = _j(runtime.handle_find_symbol("any", "main", match="contains", limit=20))
        assert d["ok"] is True
        mains = [e for e in d["entities"] if e["symbol_id"] == SYMBOL_MAIN]
        assert len(mains) == 1
        assert mains[0]["display_name"] == "main"


class TestAgentSemanticQuery:
    """Natural-language retrieval via hybrid / semantic (MCP semantic_query)."""

    def test_intent_hybrid_parse_options_hits_symbol(self, runtime: CodeindexMcpRuntime) -> None:
        """hybrid search parse_options → top results include parse_options symbol."""
        d = _j(runtime.handle_semantic_query("parse_options", mode="hybrid", top_k=10))
        assert d["ok"] is True
        ids = _collect_query_result_ids(d["results"])
        assert SYMBOL_PARSE_OPTIONS in ids, f"expected symbol id {SYMBOL_PARSE_OPTIONS!r}, got {ids}"

    def test_intent_hybrid_main_path_is_main_cc(self, runtime: CodeindexMcpRuntime) -> None:
        """hybrid search for main → payload paths should include main.cc (chunk hits)."""
        d = _j(runtime.handle_semantic_query("main", mode="hybrid", top_k=5))
        assert d["ok"] is True
        paths = _payload_paths(d["results"])
        assert any(PATH_MAIN_CC in p or p.endswith(PATH_MAIN_CC) for p in paths), (
            f"expected path containing {PATH_MAIN_CC!r}, payload.paths={paths}"
        )

    def test_intent_hybrid_hits_both_symbol_and_chunk(self, runtime: CodeindexMcpRuntime) -> None:
        """hybrid search for add → both symbol and chunk hits."""
        d = _j(runtime.handle_semantic_query("add", mode="hybrid", top_k=15))
        assert d["ok"] is True
        types = {r["type"] for r in d["results"]}
        ids = _collect_query_result_ids(d["results"])
        assert "symbol" in types and "chunk" in types, f"expected symbol+chunk, types={types}"
        assert SYMBOL_ADD in ids, f"hybrid should hit {SYMBOL_ADD!r}, ids sample={list(ids)[:8]}"


class TestAgentSymbolGraph:
    """def / call relations given symbol_id (matches relations in index)."""

    def test_intent_where_is_main_defined(self, runtime: CodeindexMcpRuntime) -> None:
        """where is main defined → def_of(main) → paths include main.cc."""
        d = _j(runtime.handle_symbol_graph("def_of", SYMBOL_MAIN, top_k=5))
        assert d["ok"] is True
        assert d["op"] == "def_of"
        paths = _payload_paths(d["results"])
        assert len(d["results"]) >= 1
        assert any(PATH_MAIN_CC in p or p.endswith(PATH_MAIN_CC) for p in paths), (
            f"def_of main should land on {PATH_MAIN_CC!r}, paths={paths}"
        )

    def test_intent_main_calls_add(self, runtime: CodeindexMcpRuntime) -> None:
        """who does main call → callees_of(main) → includes add (ndjson calls relation)."""
        d = _j(runtime.handle_symbol_graph("callees_of", SYMBOL_MAIN, top_k=10))
        assert d["ok"] is True
        ids = _collect_query_result_ids(d["results"])
        assert SYMBOL_ADD in ids, f"expected callees to include {SYMBOL_ADD!r}, got {ids}"


class TestAgentCodeGraph:
    """Call-graph subgraph (seeds use node_id)."""

    def test_intent_subgraph_main_one_hop_reaches_add(self, runtime: CodeindexMcpRuntime) -> None:
        """one hop from main → code mode + method seed → edge main→add."""
        d = _j(
            runtime.handle_code_graph_explore(
                "code",
                seed_ids=[NODE_MAIN],
                hops=1,
                edge_type="calls",
            )
        )
        assert d["ok"] is True
        data = d["data"]
        edges = data.get("edges") or []
        node_by_id = {n["node_id"]: n for n in (data.get("nodes") or [])}
        assert NODE_MAIN in node_by_id, f"expected seed node {NODE_MAIN!r}, nodes={list(node_by_id)[:5]}"
        found = any(
            str(e.get("src")) == NODE_MAIN and str(e.get("dst")) == NODE_ADD for e in edges
        ) or any(
            SYMBOL_MAIN in str(e.get("src", ""))
            and SYMBOL_ADD in str(e.get("dst", ""))
            for e in edges
        )
        assert found, f"expected calls edge main→add, edges={edges}"

    def test_intent_explore_by_symbol_resolves_like_agent_flow(self, runtime: CodeindexMcpRuntime) -> None:
        """find_symbol for SYMBOL_MAIN then explore(symbol=...) → non-empty nodes."""
        d = _j(runtime.handle_code_graph_explore("explore", symbol=SYMBOL_MAIN, hops=1, edge_type="calls"))
        assert d["ok"] is True
        data = d["data"]
        assert "nodes" in data and len(data["nodes"]) >= 1


class TestAgentInvalidInput:
    """Invalid args → structured error, not mistaken success."""

    def test_empty_query_rejected(self, runtime: CodeindexMcpRuntime) -> None:
        d = _j(runtime.handle_semantic_query("   ", mode="hybrid"))
        assert d["ok"] is False
        assert d["error"]["code"] == "INPUT_VALIDATION"
        assert d["error"]["retryable"] is False
        assert "suggested_next_steps" in d["error"]

    def test_structure_mode_rejected(self, runtime: CodeindexMcpRuntime) -> None:
        d = _j(runtime.handle_semantic_query("main", mode="structure"))
        assert d["ok"] is False
        assert d["tool"] == "semantic_query"
        assert d["error"]["code"] == "INPUT_VALIDATION"

    def test_bad_symbol_graph_op(self, runtime: CodeindexMcpRuntime) -> None:
        d = _j(runtime.handle_symbol_graph("not_an_op", SYMBOL_MAIN))
        assert d["ok"] is False
        assert d["error"]["code"] == "UNSUPPORTED_OPERATION"
        assert d["error"]["retryable"] is False

    def test_bad_entity_type(self, runtime: CodeindexMcpRuntime) -> None:
        d = _j(runtime.handle_find_symbol("class_that_does_not_exist_type", "x"))
        assert d["ok"] is False
        assert d["error"]["code"] == "INPUT_VALIDATION"


class TestAgentIntentSubgraphEmpty:
    """Without intent pipeline, intent mode returns empty graph but ok."""

    def test_intent_mode_no_communities(self, runtime: CodeindexMcpRuntime) -> None:
        d = _j(runtime.handle_code_graph_explore("intent", community_ids=[]))
        assert d["ok"] is True
        assert d["graph_mode"] == "intent"
        assert d["data"]["nodes"] == []
