from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Iterable, List, Protocol, Sequence

from .storage import SqliteStore


@dataclass(frozen=True)
class VectorHit:
    chunk_id: str
    score: float


class VectorStore(Protocol):
    def upsert_embeddings(self, embedding_version: str, vectors: Dict[str, List[float]]) -> None:
        raise NotImplementedError

    def search(self, query_vec: List[float], embedding_version: str, top_k: int) -> List[VectorHit]:
        raise NotImplementedError

    def delete_by_chunk_ids(
        self,
        chunk_ids: Sequence[str],
        embedding_version: str | None = None,
    ) -> None:
        raise NotImplementedError


def dot_product(a: List[float], b: List[float]) -> float:
    if not a or not b:
        return 0.0
    return sum(x * y for x, y in zip(a, b))


class SqliteVectorStore:
    def __init__(self, store: SqliteStore) -> None:
        self.store = store

    def upsert_embeddings(self, embedding_version: str, vectors: Dict[str, List[float]]) -> None:
        self.store.upsert_embeddings(embedding_version, vectors)

    def search(self, query_vec: List[float], embedding_version: str, top_k: int) -> List[VectorHit]:
        vectors = self.store.fetch_embeddings(embedding_version)
        scores = [VectorHit(chunk_id=chunk_id, score=dot_product(query_vec, vec)) for chunk_id, vec in vectors.items()]
        scores.sort(key=lambda x: x.score, reverse=True)
        return scores[:top_k]

    def delete_by_chunk_ids(
        self,
        chunk_ids: Sequence[str],
        embedding_version: str | None = None,
    ) -> None:
        if not chunk_ids:
            return
        q_marks = ",".join(["?"] * len(chunk_ids))
        if embedding_version is None:
            self.store.conn.execute(
                f"DELETE FROM embeddings WHERE chunk_id IN ({q_marks})",
                tuple(chunk_ids),
            )
        else:
            self.store.conn.execute(
                f"DELETE FROM embeddings WHERE embedding_version = ? AND chunk_id IN ({q_marks})",
                (embedding_version, *chunk_ids),
            )


def dedupe_vector_stores(stores: Iterable[VectorStore]) -> List[VectorStore]:
    out: List[VectorStore] = []
    seen: set[int] = set()
    for store in stores:
        key = id(store)
        if key in seen:
            continue
        seen.add(key)
        out.append(store)
    return out
