from __future__ import annotations

import time
from dataclasses import dataclass
from typing import List

from .models import OccurrenceEdge, RelationEdge, RepoSnapshot, ScipDocument, SymbolNode
from .parser import parse_scip_stream
from .storage import SqliteStore


@dataclass
class IngestionStats:
    documents: int = 0
    symbols: int = 0
    occurrences: int = 0
    relations: int = 0
    failures: int = 0


class IngestionPipeline:
    def __init__(self, store: SqliteStore, batch_size: int = 1000) -> None:
        self.store = store
        self.batch_size = batch_size

    def run(
        self,
        input_path: str,
        repo: str,
        commit: str,
        index_version: str = "v1",
        retries: int = 2,
        retry_backoff_s: float = 0.5,
        source_root: str | None = None,
    ) -> IngestionStats:
        _ = RepoSnapshot(
            repo=repo,
            commit=commit,
            index_version=index_version,
            ingested_at_epoch_ms=int(time.time() * 1000),
        )
        self.store.delete_repo_snapshot(repo, commit)

        docs: List[ScipDocument] = []
        symbols: List[SymbolNode] = []
        occs: List[OccurrenceEdge] = []
        rels: List[RelationEdge] = []
        stats = IngestionStats()

        attempt = 0
        while True:
            try:
                for rec_type, rec in parse_scip_stream(input_path, repo, commit, source_root=source_root):
                    if rec_type == "document":
                        docs.append(rec)
                        stats.documents += 1
                    elif rec_type == "symbol":
                        symbols.append(rec)
                        stats.symbols += 1
                    elif rec_type == "occurrence":
                        occs.append(rec)
                        stats.occurrences += 1
                    elif rec_type == "relation":
                        rels.append(rec)
                        stats.relations += 1

                    if len(docs) >= self.batch_size:
                        self.store.upsert_documents(repo, commit, docs)
                        docs.clear()
                    if len(symbols) >= self.batch_size:
                        self.store.upsert_symbols(symbols)
                        symbols.clear()
                    if len(occs) >= self.batch_size:
                        self.store.insert_occurrences(occs)
                        occs.clear()
                    if len(rels) >= self.batch_size:
                        self.store.insert_relations(rels)
                        rels.clear()
                break
            except Exception:
                stats.failures += 1
                if attempt >= retries:
                    raise
                time.sleep(retry_backoff_s * (2**attempt))
                attempt += 1

        if docs:
            self.store.upsert_documents(repo, commit, docs)
        if symbols:
            self.store.upsert_symbols(symbols)
        if occs:
            self.store.insert_occurrences(occs)
        if rels:
            self.store.insert_relations(rels)
        self.store.commit()
        return stats
