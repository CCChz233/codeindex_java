import json
from pathlib import Path

from hybrid_platform.index_metadata import upsert_entry
from hybrid_platform.index_slug import repo_commit_slug
from hybrid_platform.java_eval_prep import _inspect_db, build_routes, derive_targets
from hybrid_platform.storage import SqliteStore


def _write_manifest(path: Path) -> None:
    rows = [
        {
            "id": "sample-a",
            "repo": "org/repo-a",
            "repo_url": "https://github.com/org/repo-a.git",
            "base_sha": "a" * 40,
            "language": "Java",
            "difficulty": "easy",
            "task_type": "bugfix",
            "repo_type": "webdev",
        },
        {
            "id": "sample-b",
            "repo": "org/repo-a",
            "repo_url": "https://github.com/org/repo-a.git",
            "base_sha": "a" * 40,
            "language": "Java",
            "difficulty": "medium",
            "task_type": "feature-request",
            "repo_type": "webdev",
        },
        {
            "id": "sample-c",
            "repo": "org/repo-b",
            "repo_url": "https://github.com/org/repo-b.git",
            "base_sha": "b" * 40,
            "language": "Java",
            "difficulty": "hard",
            "task_type": "bugfix",
            "repo_type": "data-eng",
        },
    ]
    path.write_text("\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n", encoding="utf-8")


def _create_index_db(path: Path, *, repo: str, commit: str, symbol_name: str) -> None:
    store = SqliteStore(str(path))
    try:
        doc_id = f"{repo}:{commit}:src/main/java/com/example/{symbol_name}.java"
        symbol_id = f"semanticdb maven . . com/example/{symbol_name}#"
        store.conn.execute(
            """
            INSERT INTO documents(document_id, repo, commit_hash, relative_path, language, occurrence_count, content)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                doc_id,
                repo,
                commit,
                f"src/main/java/com/example/{symbol_name}.java",
                "java",
                1,
                f"class {symbol_name} {{}}",
            ),
        )
        store.conn.execute(
            """
            INSERT INTO symbols(symbol_id, display_name, kind, package, enclosing_symbol, language, signature_hash, symbol_fingerprint)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                symbol_id,
                symbol_name,
                "Class",
                "com/example",
                "",
                "java",
                "sig",
                "fp",
            ),
        )
        store.conn.execute(
            """
            INSERT INTO occurrences(
              document_id, symbol_id, range_start_line, range_start_col, range_end_line, range_end_col, role
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (doc_id, symbol_id, 1, 0, 1, len(symbol_name), "definition"),
        )
        store.conn.execute(
            """
            INSERT INTO chunks(chunk_id, document_id, content, primary_symbol_ids, span_start_line, span_end_line, embedding_version)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                f"{repo}:{commit}:chunk:0",
                doc_id,
                f"class {symbol_name} {{}}",
                json.dumps([symbol_id]),
                1,
                1,
                "v1",
            ),
        )
        store.conn.execute(
            """
            INSERT INTO embeddings(chunk_id, embedding_version, vector_json)
            VALUES (?, ?, ?)
            """,
            (f"{repo}:{commit}:chunk:0", "v1", "[0.1, 0.2]"),
        )
        store.conn.commit()
    finally:
        store.close()


def test_derive_targets_groups_samples_and_marks_reusable(tmp_path: Path) -> None:
    manifest = tmp_path / "manifest.jsonl"
    _write_manifest(manifest)

    worktrees_root = tmp_path / "worktrees"
    index_output_dir = tmp_path / "hybrid_indices"
    metadata_file = tmp_path / "index_metadata.json"
    config_path = tmp_path / "config.json"
    config_path.write_text("{}", encoding="utf-8")

    repo = "org/repo-a"
    commit = "a" * 40
    db_path = index_output_dir / f"{repo_commit_slug(repo, commit)}.db"
    _create_index_db(db_path, repo=repo, commit=commit, symbol_name="ExampleA")
    upsert_entry(repo, commit, str(db_path), config_path=str(config_path), metadata_path=metadata_file)

    targets, samples = derive_targets(
        manifest,
        worktrees_root=worktrees_root,
        index_output_dir=index_output_dir,
        metadata_file=metadata_file,
        config_path=config_path,
    )

    assert len(samples) == 3
    assert len(targets) == 2

    target_a = next(target for target in targets if target.repo == repo)
    assert target_a.sample_count == 2
    assert target_a.sample_ids == ["sample-a", "sample-b"]
    assert target_a.reusable_index is True
    assert target_a.index_status == "ready"
    assert target_a.effective_db_path == str(db_path.resolve())
    assert target_a.worktree_path == str((worktrees_root / repo_commit_slug(repo, commit)).resolve())

    routes = build_routes(samples, targets)
    assert len(routes) == 3
    route_a = next(route for route in routes if route["sample_id"] == "sample-a")
    assert route_a["db_path"] == str(db_path.resolve())
    assert route_a["slug"] == repo_commit_slug(repo, commit)


def test_derive_targets_applies_default_and_specific_overrides(tmp_path: Path) -> None:
    manifest = tmp_path / "manifest.jsonl"
    _write_manifest(manifest)

    worktrees_root = tmp_path / "worktrees"
    index_output_dir = tmp_path / "hybrid_indices"
    metadata_file = tmp_path / "index_metadata.json"
    config_path = tmp_path / "config.json"
    config_path.write_text("{}", encoding="utf-8")

    slug = repo_commit_slug("org/repo-b", "b" * 40)
    overrides = {
        "defaults": {
            "config_path": str((tmp_path / "deterministic.json").resolve()),
            "clone_shallow": True,
            "build_args": ["-DskipTests"],
            "build_env": {"MAVEN_OPTS": "-Xmx2g"},
        },
        "targets": {
            slug: {
                "build_tool": "gradle",
                "pilot": True,
                "notes": "gradle pilot",
                "recurse_submodules": True,
                "build_env": {"JAVA_TOOL_OPTIONS": "-Dfoo=bar"},
            }
        },
    }
    overrides_path = tmp_path / "overrides.json"
    overrides_path.write_text(json.dumps(overrides, ensure_ascii=False, indent=2), encoding="utf-8")

    targets, _ = derive_targets(
        manifest,
        worktrees_root=worktrees_root,
        index_output_dir=index_output_dir,
        metadata_file=metadata_file,
        config_path=config_path,
        overrides_path=overrides_path,
    )

    target_b = next(target for target in targets if target.slug == slug)
    assert target_b.config_path == str((tmp_path / "deterministic.json").resolve())
    assert target_b.clone_shallow is True
    assert target_b.build_args == ["-DskipTests"]
    assert target_b.build_env == {"MAVEN_OPTS": "-Xmx2g", "JAVA_TOOL_OPTIONS": "-Dfoo=bar"}
    assert target_b.build_tool == "gradle"
    assert target_b.pilot is True
    assert target_b.recurse_submodules is True
    assert target_b.notes == "gradle pilot"


def test_inspect_db_returns_counts_and_smoke_symbol(tmp_path: Path) -> None:
    db_path = tmp_path / "sample.db"
    _create_index_db(db_path, repo="org/repo-a", commit="a" * 40, symbol_name="SmokeTarget")

    counts, smoke_name, smoke_hit_count, issues = _inspect_db(db_path)

    assert not issues
    assert smoke_name == "SmokeTarget"
    assert smoke_hit_count >= 1
    assert counts["documents"] == 1
    assert counts["symbols"] == 1
    assert counts["occurrences"] == 1
    assert counts["chunks"] == 1
    assert counts["embeddings"] == 1
