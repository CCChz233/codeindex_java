"""
Spring index agent scenarios: queries and expected outputs in ``fixtures/spring_agent_golden.json``.

Before running:
  - Place an ingested+chunked+embedded Spring DB at ``/data1/qadong/spring_v6.2.10.db`` or
    ``/data/qadong/spring_v6.2.10.db``, or set ``HYBRID_SPRING_TEST_DB``.
  - Hybrid retrieval needs embedding from ``config/default_config.json`` (e.g. Voyage); structure/symbol-only tests may run offline.

Source tree paths under /data/qadong are team convention only (in golden meta); not asserted.

To inspect tool outputs, this file prints each call (long ``results``/``entities`` truncated). Run:
  ``pytest tests/test_mcp_agent_spring.py -s`` (``-s`` disables stdout capture).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from hybrid_platform.agent_mcp_handlers import CodeindexMcpRuntime

from .spring_index import spring_index_db_path, spring_test_config_path

FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures"
GOLDEN_PATH = FIXTURE_DIR / "spring_agent_golden.json"


def _load_golden() -> dict[str, Any]:
    return json.loads(GOLDEN_PATH.read_text(encoding="utf-8"))


def _j(s: str) -> dict[str, Any]:
    return json.loads(s)


def _preview_response_for_print(d: dict[str, Any], *, max_items: int = 5) -> dict[str, Any]:
    """Truncate results/entities for terminal readability; assertions use full ``d``."""
    out: dict[str, Any] = {}
    skip = frozenset({"results", "entities"})
    for k, v in d.items():
        if k in skip:
            continue
        out[k] = v
    for key in ("results", "entities"):
        if key not in d:
            continue
        lst = d[key]
        if not isinstance(lst, list):
            out[key] = lst
            continue
        n = len(lst)
        if n <= max_items:
            out[key] = lst
        else:
            out[key] = lst[:max_items]
            out[f"_{key}_preview_first"] = max_items
            out[f"_{key}_total"] = n
    return out


def _print_tool_call(scenario: dict[str, Any], tool: str, args: dict[str, Any], raw: str) -> None:
    sid = scenario.get("id", "?")
    intent = scenario.get("intent", "")
    print("\n" + "=" * 72)
    print(f"[MCP tool] {tool}  |  scenario: {sid}")
    if intent:
        print(f"  intent: {intent}")
    print("  args:")
    print(json.dumps(args, ensure_ascii=False, indent=4))
    try:
        d = json.loads(raw)
    except json.JSONDecodeError:
        print("  response (raw, JSON parse failed):")
        print(raw[:4000] + ("..." if len(raw) > 4000 else ""))
        return
    preview = _preview_response_for_print(d)
    print("  response (JSON; results/entities truncated if long):")
    print(json.dumps(preview, ensure_ascii=False, indent=2))


def _check_expect_payload_paths(results: list, substring: str) -> bool:
    sub = substring.replace("\\", "/")
    for r in results:
        pl = r.get("payload") or {}
        if not isinstance(pl, dict):
            continue
        p = pl.get("path")
        if isinstance(p, str) and sub in p.replace("\\", "/"):
            return True
    return False


def _check_expect(d: dict[str, Any], ex: dict[str, Any], *, results_key: str = "results") -> None:
    if "ok" in ex:
        assert d.get("ok") is ex["ok"], d
    if not d.get("ok"):
        return
    results = d.get(results_key) or []
    if "min_results" in ex:
        assert len(results) >= int(ex["min_results"]), f"results={results[:3]!r}..."
    if "min_entity_count" in ex:
        assert len(results) >= int(ex["min_entity_count"]), d
    if "entity_symbol_ids_contain" in ex:
        ids = {e["symbol_id"] for e in (d.get("entities") or [])}
        for sid in ex["entity_symbol_ids_contain"]:
            assert sid in ids, f"missing symbol {sid!r}, have ids={ids}"
    if "result_ids_contain" in ex:
        rids = {r["id"] for r in results}
        for rid in ex["result_ids_contain"]:
            assert rid in rids, f"missing result id {rid!r}, sample={list(rids)[:5]}"
    if "result_types_contain" in ex:
        types = {r["type"] for r in results}
        for t in ex["result_types_contain"]:
            assert t in types, f"missing type {t!r}, have types={types}"
    if "any_payload_path_contains" in ex:
        assert _check_expect_payload_paths(results, ex["any_payload_path_contains"]), (
            f"no payload.path contains {ex['any_payload_path_contains']!r}"
        )
    if "any_result_id_contains" in ex:
        needle = ex["any_result_id_contains"]
        assert any(needle in str(r.get("id", "")) for r in results), (
            f"no result.id contains {needle!r}"
        )


@pytest.fixture(scope="module")
def spring_runtime() -> CodeindexMcpRuntime:
    db = spring_index_db_path()
    if not db:
        pytest.skip(
            "Spring index DB not found: set HYBRID_SPRING_TEST_DB or place spring_v6.2.10.db under "
            "data1/qadong or data/qadong"
        )
    return CodeindexMcpRuntime(db, spring_test_config_path())


@pytest.fixture(scope="module")
def golden() -> dict[str, Any]:
    assert GOLDEN_PATH.is_file(), f"missing {GOLDEN_PATH}"
    return _load_golden()


class TestSpringGoldenScenarios:
    """Run golden scenarios from spring_agent_golden.json."""

    @pytest.mark.parametrize(
        "scenario",
        [
            pytest.param(s, id=s["id"])
            for s in _load_golden()["scenarios"]
        ],
    )
    def test_scenario(self, spring_runtime: CodeindexMcpRuntime, scenario: dict[str, Any]) -> None:
        tool = scenario["tool"]
        args = dict(scenario["args"])
        ex = scenario["expect"]
        if tool == "find_symbol":
            raw = spring_runtime.handle_find_symbol(
                entity_type=args["entity_type"],
                name=args["name"],
                match=args.get("match", "contains"),
                package_contains=args.get("package_contains", ""),
                limit=int(args.get("limit", 50)),
            )
            _print_tool_call(scenario, tool, args, raw)
            d = _j(raw)
            _check_expect(d, ex, results_key="entities")
        elif tool == "semantic_query":
            raw = spring_runtime.handle_semantic_query(
                query=args["query"],
                mode=args.get("mode", "hybrid"),
                top_k=int(args.get("top_k", 10)),
                blend_strategy=args.get("blend_strategy", "linear"),
                include_code=bool(args.get("include_code", False)),
                max_code_chars=int(args.get("max_code_chars", 1200)),
                embedding_version=args.get("embedding_version"),
            )
            _print_tool_call(scenario, tool, args, raw)
            d = _j(raw)
            _check_expect(d, ex)
        elif tool == "symbol_graph":
            raw = spring_runtime.handle_symbol_graph(
                op=args["op"],
                symbol_id=args["symbol_id"],
                top_k=int(args.get("top_k", 10)),
                include_code=bool(args.get("include_code", False)),
                max_code_chars=int(args.get("max_code_chars", 1200)),
                embedding_version=args.get("embedding_version"),
            )
            _print_tool_call(scenario, tool, args, raw)
            d = _j(raw)
            _check_expect(d, ex)
        else:
            pytest.fail(f"unknown tool {tool}")


class TestSpringMetaConsistency:
    """DB repo/commit must match golden meta (catch wrong DB)."""

    def test_db_matches_golden_repo_commit(self, golden: dict[str, Any]) -> None:
        db = spring_index_db_path()
        if not db:
            pytest.skip("no Spring DB")
        import sqlite3

        meta = golden["meta"]
        con = sqlite3.connect(db)
        try:
            row = con.execute(
                "SELECT DISTINCT repo, commit_hash FROM documents LIMIT 1"
            ).fetchone()
            assert row is not None
            repo, commit = row[0], row[1]
            assert repo == meta["repo"], f"golden repo={meta['repo']!r} actual={repo!r}"
            assert commit == meta["commit"], f"golden commit={meta['commit']!r} actual={commit!r}"
        finally:
            con.close()


class TestSpringSymbolsDocumented:
    """symbol_id entries in golden must exist in the DB."""

    def test_documented_symbols_exist(self, golden: dict[str, Any]) -> None:
        db = spring_index_db_path()
        if not db:
            pytest.skip("no Spring DB")
        import sqlite3

        con = sqlite3.connect(db)
        try:
            for name, sid in golden["symbols"].items():
                row = con.execute(
                    "SELECT 1 FROM symbols WHERE symbol_id = ? LIMIT 1",
                    (sid,),
                ).fetchone()
                assert row is not None, f"symbols.{name} not in DB: {sid[:80]}..."
        finally:
            con.close()
