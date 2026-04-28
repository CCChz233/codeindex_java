from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from hybrid_platform.models import Chunk, ScipDocument
from hybrid_platform.retrieval_compare_eval import (
    load_retrieval_compare_cases,
    run_retrieval_compare_eval,
)
from hybrid_platform.storage import SqliteStore

from .conftest import COMMIT, REPO


class FakeDensePipeline:
    def semantic_search(
        self,
        query: str,
        embedding_version: str,
        top_k: int,
    ) -> list[tuple[str, float]]:
        _ = query, embedding_version
        return [("chunk-view", 0.95), ("chunk-buffer", 0.9)][:top_k]


def _write_jsonl(path: Path, rows: list[dict]) -> str:
    path.write_text(
        "\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n",
        encoding="utf-8",
    )
    return str(path)


def _build_compare_db(db_path: Path) -> None:
    store = SqliteStore(str(db_path))
    try:
        store.prepare_index(REPO, COMMIT, source_mode="scip")
        store.upsert_documents(
            REPO,
            COMMIT,
            [
                ScipDocument(
                    document_id="doc-buffer",
                    relative_path="src/Buffer.java",
                    language="Java",
                    occurrence_count=1,
                    content="class Buffer {}",
                ),
                ScipDocument(
                    document_id="doc-view",
                    relative_path="src/View.java",
                    language="Java",
                    occurrence_count=1,
                    content="class View {}",
                ),
            ],
        )
        store.upsert_chunks(
            [
                Chunk(
                    chunk_id="chunk-buffer",
                    document_id="doc-buffer",
                    content="capacity expansion expands byte buffer",
                    primary_symbol_ids=["com.acme.Buffer#expand/0"],
                    span_start_line=1,
                    span_end_line=8,
                    embedding_version="v1",
                ),
                Chunk(
                    chunk_id="chunk-view",
                    document_id="doc-view",
                    content="render view template",
                    primary_symbol_ids=["com.acme.View#render/0"],
                    span_start_line=1,
                    span_end_line=5,
                    embedding_version="v1",
                ),
            ]
        )
        store.commit()
    finally:
        store.close()


def test_load_retrieval_compare_cases_accepts_reviewed_flat_jsonl(tmp_path: Path) -> None:
    dataset = _write_jsonl(
        tmp_path / "spring.verify.jsonl",
        [
            {
                "sample_id": "spring_case",
                "query": "where is capacity expanded",
                "gold_files": "src/Buffer.java | src/Other.java",
                "gold_symbols": "com.acme.Buffer#expand/0",
                "repo_sha": COMMIT,
            }
        ],
    )

    cases = load_retrieval_compare_cases(dataset)

    assert len(cases) == 1
    assert cases[0].case_id == "spring_case"
    assert cases[0].query == "where is capacity expanded"
    assert cases[0].gold_files == ("src/Buffer.java", "src/Other.java")
    assert cases[0].gold_symbols == ("com.acme.Buffer#expand/0",)
    assert cases[0].repo_sha == COMMIT


def test_run_retrieval_compare_eval_filters_commit_and_scores(tmp_path: Path) -> None:
    db = tmp_path / "compare.db"
    _build_compare_db(db)
    dataset = _write_jsonl(
        tmp_path / "compare.jsonl",
        [
            {
                "sample_id": "hit",
                "query": "capacity expansion",
                "gold_files": "src/Buffer.java",
                "gold_symbols": "com.acme.Buffer#expand/0",
                "repo_sha": COMMIT,
            },
            {
                "sample_id": "skip",
                "query": "capacity expansion",
                "gold_files": "src/Buffer.java",
                "repo_sha": "different-sha",
            },
        ],
    )

    store = SqliteStore(str(db))
    try:
        report = run_retrieval_compare_eval(
            store=store,
            embedding_pipeline=FakeDensePipeline(),
            dataset_path=dataset,
            repo=REPO,
            commit=COMMIT,
            embedding_version="v1",
            top_ks=[1, 2],
        )
    finally:
        store.close()

    assert report["summary"]["loaded_cases"] == 2
    assert report["summary"]["evaluated_cases"] == 1
    assert report["summary"]["skipped_commit_mismatch"] == 1
    assert report["summary"]["dense"]["recall@1"] == 0.0
    assert report["summary"]["dense"]["mrr@2"] == 0.5
    assert report["summary"]["bm25"]["recall@1"] == 1.0
    assert report["summary"]["bm25"]["mrr@1"] == 1.0
    assert report["cases"][0]["dense"]["failure_reason"] == ""
    assert report["cases"][0]["bm25"]["failure_reason"] == ""
    assert "Recall@1" in report["table_markdown"]


def test_eval_retrieval_compare_cli_writes_output(
    mcp_fixture_db: tuple[str, str],
    tmp_path: Path,
) -> None:
    db_path, config_path = mcp_fixture_db
    dataset = _write_jsonl(
        tmp_path / "compare-cli.jsonl",
        [
            {
                "sample_id": "cli-case",
                "query": "parse_options",
                "gold_files": "src/main.cc",
                "repo_sha": COMMIT,
            }
        ],
    )
    output = tmp_path / "compare-report.json"
    project_root = Path(__file__).resolve().parents[1]

    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "hybrid_platform.cli",
            "--config",
            config_path,
            "eval-retrieval-compare",
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
            "--top-k",
            "10",
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
    assert set(report.keys()) >= {"summary", "table_markdown", "cases", "index_info"}
    assert report["summary"]["loaded_cases"] == 1
    assert report["summary"]["evaluated_cases"] == 1
    assert "Recall@5" in report["table_markdown"]
