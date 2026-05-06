from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, Sequence

from .fallback_indexer import DocumentFallbackIndexer, SyntaxFallbackIndexer
from .index_contract import (
    SOURCE_BACKEND_DOCUMENT,
    SOURCE_BACKEND_SCIP_JAVA,
    SOURCE_BACKEND_TREE_SITTER_JAVA,
    SOURCE_MODE_DOCUMENT,
    SOURCE_MODE_SCIP,
    SOURCE_MODE_SYNTAX,
    capabilities_for_source_mode,
)
from .ingestion import IngestionPipeline
from .java_indexer import JavaIndexRequest, JavaIndexer
from .storage import SqliteStore

SOURCE_BACKEND_VERSION = "v1"


@dataclass(frozen=True)
class SourceIndexResult:
    source_backend: str
    source_mode: str
    ingest: dict[str, object]
    backend_stats: dict[str, object]
    build_failure: dict[str, object] | None = None


class SourceIndexer(Protocol):
    source_backend: str

    def run(self, store: SqliteStore) -> SourceIndexResult:
        raise NotImplementedError


class ScipJavaSourceIndexer:
    source_backend = SOURCE_BACKEND_SCIP_JAVA

    def __init__(
        self,
        *,
        java_request: JavaIndexRequest,
        repo: str,
        commit: str,
        index_version: str,
        batch_size: int,
        retries: int,
        source_root: str,
    ) -> None:
        self.java_request = java_request
        self.repo = repo
        self.commit = commit
        self.index_version = index_version
        self.batch_size = batch_size
        self.retries = retries
        self.source_root = source_root

    def run(self, store: SqliteStore) -> SourceIndexResult:
        java_res = JavaIndexer(self.java_request).run()
        backend_stats = {
            "build_tool": java_res.build_tool,
            "command": java_res.command,
            "output_path": java_res.output_path,
            "elapsed_ms": java_res.elapsed_ms,
            "used_manual_fallback": java_res.used_manual_fallback,
            "stdout_chars": len(java_res.stdout or ""),
            "stderr_chars": len(java_res.stderr or ""),
        }
        ingest_stats = IngestionPipeline(store, batch_size=self.batch_size).run(
            input_path=java_res.output_path,
            repo=self.repo,
            commit=self.commit,
            index_version=self.index_version,
            retries=self.retries,
            source_root=self.source_root,
            source_mode=SOURCE_MODE_SCIP,
            build_tool=java_res.build_tool,
            source_backend=SOURCE_BACKEND_SCIP_JAVA,
            backend_version=SOURCE_BACKEND_VERSION,
            backend_stats=backend_stats,
            capabilities=capabilities_for_source_mode(SOURCE_MODE_SCIP),
        )
        return SourceIndexResult(
            source_backend=SOURCE_BACKEND_SCIP_JAVA,
            source_mode=SOURCE_MODE_SCIP,
            ingest=ingest_stats.__dict__,
            backend_stats=backend_stats,
        )


class TreeSitterJavaSourceIndexer:
    source_backend = SOURCE_BACKEND_TREE_SITTER_JAVA

    def __init__(self, *, repo_root: str, repo: str, commit: str, build_failure: dict[str, object] | None = None) -> None:
        self.repo_root = repo_root
        self.repo = repo
        self.commit = commit
        self.build_failure = build_failure

    def run(self, store: SqliteStore) -> SourceIndexResult:
        store.prepare_index(
            self.repo,
            self.commit,
            source_mode=SOURCE_MODE_SYNTAX,
            build_tool="tree-sitter-java",
            build_failure=self.build_failure,
            source_backend=SOURCE_BACKEND_TREE_SITTER_JAVA,
            backend_version=SOURCE_BACKEND_VERSION,
            capabilities=capabilities_for_source_mode(SOURCE_MODE_SYNTAX),
        )
        stats = SyntaxFallbackIndexer(store).run(self.repo_root, self.repo, self.commit)
        backend_stats = stats.as_dict()
        store.prepare_index(
            self.repo,
            self.commit,
            source_mode=SOURCE_MODE_SYNTAX,
            build_tool="tree-sitter-java",
            build_failure=self.build_failure,
            source_backend=SOURCE_BACKEND_TREE_SITTER_JAVA,
            backend_version=SOURCE_BACKEND_VERSION,
            backend_stats=backend_stats,
            capabilities=capabilities_for_source_mode(SOURCE_MODE_SYNTAX),
        )
        store.commit()
        return SourceIndexResult(
            source_backend=SOURCE_BACKEND_TREE_SITTER_JAVA,
            source_mode=SOURCE_MODE_SYNTAX,
            ingest={
                "documents": stats.documents,
                "symbols": stats.symbols,
                "occurrences": stats.occurrences,
                "relations": stats.relations,
                "failures": 0,
                "source_mode": stats.source_mode,
            },
            backend_stats=backend_stats,
            build_failure=self.build_failure,
        )


class DocumentSourceIndexer:
    source_backend = SOURCE_BACKEND_DOCUMENT

    def __init__(self, *, repo_root: str, repo: str, commit: str, build_failure: dict[str, object] | None = None) -> None:
        self.repo_root = repo_root
        self.repo = repo
        self.commit = commit
        self.build_failure = build_failure

    def run(self, store: SqliteStore) -> SourceIndexResult:
        store.prepare_index(
            self.repo,
            self.commit,
            source_mode=SOURCE_MODE_DOCUMENT,
            build_tool="document",
            build_failure=self.build_failure,
            source_backend=SOURCE_BACKEND_DOCUMENT,
            backend_version=SOURCE_BACKEND_VERSION,
            capabilities=capabilities_for_source_mode(SOURCE_MODE_DOCUMENT),
        )
        stats = DocumentFallbackIndexer(store).run(self.repo_root, self.repo, self.commit)
        backend_stats = stats.as_dict()
        store.prepare_index(
            self.repo,
            self.commit,
            source_mode=SOURCE_MODE_DOCUMENT,
            build_tool="document",
            build_failure=self.build_failure,
            source_backend=SOURCE_BACKEND_DOCUMENT,
            backend_version=SOURCE_BACKEND_VERSION,
            backend_stats=backend_stats,
            capabilities=capabilities_for_source_mode(SOURCE_MODE_DOCUMENT),
        )
        store.commit()
        return SourceIndexResult(
            source_backend=SOURCE_BACKEND_DOCUMENT,
            source_mode=SOURCE_MODE_DOCUMENT,
            ingest={
                "documents": stats.documents,
                "symbols": stats.symbols,
                "occurrences": stats.occurrences,
                "relations": stats.relations,
                "failures": 0,
                "source_mode": stats.source_mode,
            },
            backend_stats=backend_stats,
            build_failure=self.build_failure,
        )


def capabilities_for_backend(source_backend: str) -> Sequence[str]:
    if source_backend == SOURCE_BACKEND_DOCUMENT:
        return capabilities_for_source_mode(SOURCE_MODE_DOCUMENT)
    if source_backend == SOURCE_BACKEND_TREE_SITTER_JAVA:
        return capabilities_for_source_mode(SOURCE_MODE_SYNTAX)
    return capabilities_for_source_mode(SOURCE_MODE_SCIP)
