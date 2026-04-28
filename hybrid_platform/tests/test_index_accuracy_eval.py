from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from hybrid_platform.index_accuracy_eval import load_accuracy_cases, run_index_accuracy_eval
from hybrid_platform.retrieval import HybridRetrievalService
from hybrid_platform.storage import SqliteStore

from .conftest import COMMIT, REPO

SYMBOL_ADD = "scip-cpp demo add()."
SYMBOL_MAIN = "scip-cpp demo main()."
SYMBOL_PARSE_OPTIONS = "scip-cpp demo parse_options()."


def _write_jsonl(path: Path, rows: list[dict]) -> str:
    path.write_text(
        "\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n",
        encoding="utf-8",
    )
    return str(path)


def test_load_accuracy_cases_validates_jsonl(tmp_path: Path) -> None:
    dataset = tmp_path / "bad.jsonl"
    dataset.write_text(json.dumps({"id": "x", "kind": "unknown"}) + "\n", encoding="utf-8")

    with pytest.raises(ValueError, match="unsupported kind"):
        load_accuracy_cases(str(dataset))


def test_load_accuracy_cases_accepts_spring_reviewed_flat_jsonl(tmp_path: Path) -> None:
    dataset = tmp_path / "spring.verify.jsonl"
    dataset.write_text(
        json.dumps(
            {
                "sample_id": "spring_case",
                "query": "Find all declarations annotated with @Autowired.",
                "gold_files": "a/A.java | b/B.java",
                "gold_symbols": "com.acme.A#set/1 | com.acme.B#set/1",
            }
        )
        + "\n",
        encoding="utf-8",
    )

    cases = load_accuracy_cases(str(dataset))

    assert len(cases) == 1
    assert cases[0].case_id == "spring_case"
    assert cases[0].kind == "retrieval"
    assert cases[0].raw["expected"] == {"files": ["a/A.java", "b/B.java"]}
    assert cases[0].raw["source_format"] == "spring_reviewed_flat"


def test_run_index_accuracy_eval_entity_retrieval_graph(mcp_fixture_db: tuple[str, str], tmp_path: Path) -> None:
    db_path, config_path = mcp_fixture_db
    _ = config_path
    dataset = _write_jsonl(
        tmp_path / "eval.jsonl",
        [
            {
                "id": "entity-add",
                "kind": "entity",
                "entity_query": {"type": "any", "name": "add", "match": "exact"},
                "expected": {"symbols": [SYMBOL_ADD]},
            },
            {
                "id": "retrieval-parse-options",
                "kind": "retrieval",
                "query": "parse_options",
                "mode": "hybrid",
                "expected": {"symbols": [SYMBOL_PARSE_OPTIONS]},
            },
            {
                "id": "graph-main-callees",
                "kind": "graph",
                "op": "callees_of",
                "symbol_id": SYMBOL_MAIN,
                "expected": {"symbols": [SYMBOL_ADD]},
            },
        ],
    )

    store = SqliteStore(db_path)
    try:
        service = HybridRetrievalService(store)
        report = run_index_accuracy_eval(
            store=store,
            service=service,
            dataset_path=dataset,
            repo=REPO,
            commit=COMMIT,
            top_k=10,
            mode="hybrid",
        )
    finally:
        store.close()

    assert set(report.keys()) >= {"summary", "by_kind", "cases", "index_info"}
    assert report["summary"]["samples"] == 3
    assert report["summary"]["kind_counts"] == {"entity": 1, "graph": 1, "retrieval": 1}
    assert report["summary"]["success@k"] == 1.0
    assert report["summary"]["unsupported_count"] == 0
    assert {case["id"]: case["is_relevant"] for case in report["cases"]} == {
        "entity-add": True,
        "retrieval-parse-options": True,
        "graph-main-callees": True,
    }


def test_eval_index_accuracy_cli_writes_output(
    mcp_fixture_db: tuple[str, str],
    tmp_path: Path,
) -> None:
    db_path, config_path = mcp_fixture_db
    dataset = _write_jsonl(
        tmp_path / "eval-cli.jsonl",
        [
            {
                "id": "graph-def-main",
                "kind": "graph",
                "op": "def_of",
                "symbol_id": SYMBOL_MAIN,
                "expected": {"files": ["src/main.cc"]},
            }
        ],
    )
    output = tmp_path / "report.json"
    project_root = Path(__file__).resolve().parents[1]

    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "hybrid_platform.cli",
            "--config",
            config_path,
            "eval-index-accuracy",
            "--db",
            db_path,
            "--repo",
            REPO,
            "--commit",
            COMMIT,
            "--dataset",
            dataset,
            "--top-k",
            "5",
            "--output",
            str(output),
        ],
        cwd=str(project_root),
        capture_output=True,
        text=True,
        check=False,
    )

    assert proc.returncode == 0, proc.stderr
    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["summary"]["samples"] == 1
    assert report["summary"]["success@k"] == 1.0
    assert report["cases"][0]["failure_reason"] == ""


def test_index_accuracy_reports_unsupported_capability(tmp_path: Path) -> None:
    db = tmp_path / "syntax.db"
    dataset = _write_jsonl(
        tmp_path / "unsupported.jsonl",
        [
            {
                "id": "calls-not-supported",
                "kind": "graph",
                "op": "callees_of",
                "symbol_id": "ts:Demo.java#Demo.main:method:0",
                "expected": {"symbols": ["ts:Demo.java#Demo.add:method:0"]},
            }
        ],
    )
    store = SqliteStore(str(db))
    try:
        store.prepare_index(REPO, COMMIT, source_mode="syntax")
        service = HybridRetrievalService(store)
        report = run_index_accuracy_eval(
            store=store,
            service=service,
            dataset_path=dataset,
            repo=REPO,
            commit=COMMIT,
            top_k=10,
            mode="hybrid",
        )
    finally:
        store.close()

    assert report["summary"]["unsupported_count"] == 1
    assert report["by_kind"]["graph"]["unsupported_count"] == 1
    assert report["cases"][0]["failure_reason"] == "unsupported_capability"
    assert report["cases"][0]["error"]["capability"] == "call"
