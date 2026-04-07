from __future__ import annotations

from typing import Dict, List, Sequence

from .vector_store import VectorHit


def _sql_quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


class LanceDbVectorStore:
    def __init__(
        self,
        uri: str,
        table: str = "chunk_vectors",
        metric: str = "cosine",
    ) -> None:
        try:
            import lancedb  # type: ignore
            import pyarrow as pa  # type: ignore
        except Exception as exc:  # pragma: no cover
            raise RuntimeError(
                "使用 LanceDB 需要先安装依赖：pip install lancedb pyarrow"
            ) from exc
        self._lancedb = lancedb
        self._pa = pa
        self.uri = uri
        self.table_name = table
        self.metric = metric
        self.db = self._lancedb.connect(uri)
        self._table_handle = None

    def _table_exists(self) -> bool:
        return self.table_name in set(self.db.table_names())

    def _ensure_table(self, dim: int) -> object:
        if self._table_handle is not None:
            return self._table_handle
        if self._table_exists():
            self._table_handle = self.db.open_table(self.table_name)
            return self._table_handle
        schema = self._pa.schema(
            [
                self._pa.field("chunk_id", self._pa.string(), nullable=False),
                self._pa.field("embedding_version", self._pa.string(), nullable=False),
                self._pa.field("vector", self._pa.list_(self._pa.float32(), dim), nullable=False),
            ]
        )
        self._table_handle = self.db.create_table(self.table_name, schema=schema)
        return self._table_handle

    def _get_table(self) -> object:
        if self._table_handle is None:
            if self._table_exists():
                self._table_handle = self.db.open_table(self.table_name)
        return self._table_handle

    def upsert_embeddings(self, embedding_version: str, vectors: Dict[str, List[float]]) -> None:
        if not vectors:
            return
        dim = len(next(iter(vectors.values())))
        table = self._ensure_table(dim)
        rows = [
            {
                "chunk_id": chunk_id,
                "embedding_version": embedding_version,
                "vector": [float(x) for x in vec],
            }
            for chunk_id, vec in vectors.items()
        ]
        # 批量 upsert：按 (chunk_id, embedding_version) 匹配则整行更新，否则插入；避免先 delete 再 add 的二次扫描。
        merge_insert = getattr(table, "merge_insert", None)
        if merge_insert is not None:
            merge_insert(["chunk_id", "embedding_version"]).when_matched_update_all().when_not_matched_insert_all().execute(
                rows
            )
        else:  # pragma: no cover
            chunk_ids = list(vectors.keys())
            self.delete_by_chunk_ids(chunk_ids, embedding_version=embedding_version)
            table.add(rows)

    def search(self, query_vec: List[float], embedding_version: str, top_k: int) -> List[VectorHit]:
        if not self._table_exists():
            return []
        table = self._get_table()
        search = table.search([float(x) for x in query_vec])
        if self.metric:
            try:
                search = search.metric(self.metric)
            except Exception:
                pass
        where_expr = f"embedding_version = {_sql_quote(embedding_version)}"
        try:
            search = search.where(where_expr, prefilter=True)
        except TypeError:
            search = search.where(where_expr)
        rows = search.limit(max(1, int(top_k))).to_list()
        hits: List[VectorHit] = []
        for row in rows:
            distance = row.get("_distance")
            if distance is None:
                distance = row.get("distance", 0.0)
            # LanceDB 返回 distance（越小越近），统一成“越大越好”的 score。
            score = -float(distance)
            hits.append(VectorHit(chunk_id=str(row["chunk_id"]), score=score))
        hits.sort(key=lambda x: x.score, reverse=True)
        return hits[:top_k]

    def delete_by_chunk_ids(
        self,
        chunk_ids: Sequence[str],
        embedding_version: str | None = None,
    ) -> None:
        if not chunk_ids:
            return
        if not self._table_exists():
            return
        table = self._get_table()
        chunk_ids = [str(c) for c in chunk_ids]
        batch_size = 500
        for i in range(0, len(chunk_ids), batch_size):
            batch = chunk_ids[i : i + batch_size]
            in_expr = ", ".join(_sql_quote(x) for x in batch)
            predicate = f"chunk_id IN ({in_expr})"
            if embedding_version is not None:
                predicate = f"embedding_version = {_sql_quote(embedding_version)} AND {predicate}"
            table.delete(predicate)

    def delete_by_chunk_id_prefix(self, prefix: str, embedding_version: str | None = None) -> None:
        """按 chunk_id 前缀删除（ingest 里 document_id = repo:commit:relative_path，chunk_id 以 document_id 为前缀）。"""
        if not prefix or not self._table_exists():
            return
        table = self._get_table()
        # 仅对字面量前缀 + 通配符；若 repo/commit 中含 % 或 _，需另行转义（极少见）
        pattern = prefix + "%"
        predicate = f"chunk_id LIKE {_sql_quote(pattern)}"
        if embedding_version is not None:
            predicate = f"embedding_version = {_sql_quote(embedding_version)} AND ({predicate})"
        table.delete(predicate)

    def drop_table_if_exists(self) -> None:
        """删除整张向量表（元数据级操作，百万行场景下远快于 table.delete 逐批重写数据文件）。"""
        self._table_handle = None
        if not self._table_exists():
            return
        self.db.drop_table(self.table_name)
