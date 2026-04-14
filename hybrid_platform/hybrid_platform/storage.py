from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Sequence

from .entity_query import EntityHit
from .models import Chunk, OccurrenceEdge, QueryResult, RelationEdge, ScipDocument, SymbolNode


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS documents (
  document_id TEXT PRIMARY KEY,
  repo TEXT NOT NULL,
  commit_hash TEXT NOT NULL,
  relative_path TEXT NOT NULL,
  language TEXT NOT NULL,
  occurrence_count INTEGER NOT NULL,
  content TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_documents_repo_commit ON documents(repo, commit_hash);

CREATE TABLE IF NOT EXISTS symbols (
  symbol_id TEXT PRIMARY KEY,
  display_name TEXT NOT NULL,
  kind TEXT NOT NULL,
  package TEXT NOT NULL,
  enclosing_symbol TEXT NOT NULL DEFAULT '',
  language TEXT NOT NULL DEFAULT '',
  signature_hash TEXT NOT NULL,
  symbol_fingerprint TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_symbols_display_name ON symbols(display_name);

CREATE TABLE IF NOT EXISTS occurrences (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  document_id TEXT NOT NULL,
  symbol_id TEXT NOT NULL,
  range_start_line INTEGER NOT NULL,
  range_start_col INTEGER NOT NULL,
  range_end_line INTEGER NOT NULL,
  range_end_col INTEGER NOT NULL,
  role TEXT NOT NULL,
  syntax_kind TEXT NOT NULL DEFAULT '',
  enclosing_range_start_line INTEGER NOT NULL DEFAULT -1,
  enclosing_range_start_col INTEGER NOT NULL DEFAULT -1,
  enclosing_range_end_line INTEGER NOT NULL DEFAULT -1,
  enclosing_range_end_col INTEGER NOT NULL DEFAULT -1
);
CREATE INDEX IF NOT EXISTS idx_occurrences_symbol ON occurrences(symbol_id);
CREATE INDEX IF NOT EXISTS idx_occurrences_doc ON occurrences(document_id);

CREATE TABLE IF NOT EXISTS relations (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  from_symbol TEXT NOT NULL,
  to_symbol TEXT NOT NULL,
  relation_type TEXT NOT NULL,
  confidence REAL NOT NULL,
  evidence_document_id TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_relations_from ON relations(from_symbol);
CREATE INDEX IF NOT EXISTS idx_relations_to ON relations(to_symbol);

CREATE TABLE IF NOT EXISTS chunks (
  chunk_id TEXT PRIMARY KEY,
  document_id TEXT NOT NULL,
  content TEXT NOT NULL,
  primary_symbol_ids TEXT NOT NULL,
  span_start_line INTEGER NOT NULL,
  span_end_line INTEGER NOT NULL,
  embedding_version TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_chunks_doc ON chunks(document_id);

CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
  chunk_id UNINDEXED,
  content
);

CREATE TABLE IF NOT EXISTS embeddings (
  chunk_id TEXT NOT NULL,
  embedding_version TEXT NOT NULL,
  vector_json TEXT NOT NULL,
  PRIMARY KEY(chunk_id, embedding_version)
);
"""


class SqliteStore:
    def __init__(self, db_path: str) -> None:
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self.db_path = str(Path(db_path))
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self._vector_delete_hook: Callable[[List[str]], None] | None = None
        self.conn.executescript(SCHEMA_SQL)
        self._migrate_schema()
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def set_vector_delete_hook(self, hook: Callable[[List[str]], None] | None) -> None:
        self._vector_delete_hook = hook

    def _ensure_column(self, table: str, column: str, ddl: str) -> None:
        cur = self.conn.execute(f"PRAGMA table_info({table})")
        cols = {str(row["name"]) for row in cur.fetchall()}
        if column not in cols:
            self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")

    def _migrate_schema(self) -> None:
        self._ensure_column("symbols", "enclosing_symbol", "TEXT NOT NULL DEFAULT ''")
        self._ensure_column("symbols", "language", "TEXT NOT NULL DEFAULT ''")
        self._ensure_column("occurrences", "syntax_kind", "TEXT NOT NULL DEFAULT ''")
        self._ensure_column("occurrences", "enclosing_range_start_line", "INTEGER NOT NULL DEFAULT -1")
        self._ensure_column("occurrences", "enclosing_range_start_col", "INTEGER NOT NULL DEFAULT -1")
        self._ensure_column("occurrences", "enclosing_range_end_line", "INTEGER NOT NULL DEFAULT -1")
        self._ensure_column("occurrences", "enclosing_range_end_col", "INTEGER NOT NULL DEFAULT -1")

    def _table_exists(self, table_name: str) -> bool:
        row = self.conn.execute(
            """
            SELECT 1
            FROM sqlite_master
            WHERE type = 'table' AND name = ?
            LIMIT 1
            """,
            (table_name,),
        ).fetchone()
        return row is not None

    def delete_repo_snapshot(self, repo: str, commit: str) -> None:
        cur = self.conn.execute(
            "SELECT document_id FROM documents WHERE repo = ? AND commit_hash = ?",
            (repo, commit),
        )
        doc_ids = [str(row["document_id"]) for row in cur.fetchall()]
        if not doc_ids:
            return
        batch_size = 400
        for i in range(0, len(doc_ids), batch_size):
            batch = doc_ids[i : i + batch_size]
            q_marks = ",".join(["?"] * len(batch))
            if self._vector_delete_hook:
                cur = self.conn.execute(
                    f"SELECT chunk_id FROM chunks WHERE document_id IN ({q_marks})",
                    tuple(batch),
                )
                chunk_ids = [str(row["chunk_id"]) for row in cur.fetchall()]
                if chunk_ids:
                    self._vector_delete_hook(chunk_ids)
            self.conn.execute(
                f"DELETE FROM relations WHERE evidence_document_id IN ({q_marks})",
                tuple(batch),
            )
            self.conn.execute(
                f"DELETE FROM occurrences WHERE document_id IN ({q_marks})",
                tuple(batch),
            )
            self.conn.execute(
                f"DELETE FROM embeddings WHERE chunk_id IN (SELECT chunk_id FROM chunks WHERE document_id IN ({q_marks}))",
                tuple(batch),
            )
            self.conn.execute(
                f"DELETE FROM chunks_fts WHERE rowid IN (SELECT rowid FROM chunks WHERE document_id IN ({q_marks}))",
                tuple(batch),
            )
            self.conn.execute(
                f"DELETE FROM chunks WHERE document_id IN ({q_marks})",
                tuple(batch),
            )
            self.conn.execute(
                f"DELETE FROM documents WHERE document_id IN ({q_marks})",
                tuple(batch),
            )

    def delete_chunks_for_repo_commit(
        self,
        repo: str,
        commit: str,
        *,
        invoke_vector_hook: bool = True,
    ) -> int:
        """删除某快照下所有 chunk / embeddings / FTS 行；可选按批触发 vector_delete_hook。不删 documents/symbols。

        使用子查询删除，避免数十万 document_id / chunk_id 塞进 Python 与 SQLite 变量上限问题。
        若已由调用方用 LanceDB 等按前缀清理向量，可传 invoke_vector_hook=False。
        """
        cur = self.conn.execute(
            """
            SELECT COUNT(*) AS cnt FROM chunks c
            INNER JOIN documents d ON d.document_id = c.document_id
            WHERE d.repo = ? AND d.commit_hash = ?
            """,
            (repo, commit),
        )
        row = cur.fetchone()
        n = int(row["cnt"] if row is not None else 0)
        if n == 0:
            return 0

        if invoke_vector_hook and self._vector_delete_hook:
            cur = self.conn.execute(
                """
                SELECT c.chunk_id FROM chunks c
                INNER JOIN documents d ON d.document_id = c.document_id
                WHERE d.repo = ? AND d.commit_hash = ?
                """,
                (repo, commit),
            )
            batch_size = 4096
            while True:
                rows = cur.fetchmany(batch_size)
                if not rows:
                    break
                self._vector_delete_hook([str(r["chunk_id"]) for r in rows])

        self.conn.execute(
            """
            DELETE FROM embeddings WHERE chunk_id IN (
              SELECT c.chunk_id FROM chunks c
              INNER JOIN documents d ON d.document_id = c.document_id
              WHERE d.repo = ? AND d.commit_hash = ?
            )
            """,
            (repo, commit),
        )
        # 按快照删 FTS 行，避免全表 NOT IN (SELECT rowid FROM chunks) 在超大规模下极耗内存
        self.conn.execute(
            """
            DELETE FROM chunks_fts WHERE rowid IN (
              SELECT c.rowid FROM chunks c
              INNER JOIN documents d ON d.document_id = c.document_id
              WHERE d.repo = ? AND d.commit_hash = ?
            )
            """,
            (repo, commit),
        )
        self.conn.execute(
            """
            DELETE FROM chunks WHERE document_id IN (
              SELECT document_id FROM documents WHERE repo = ? AND commit_hash = ?
            )
            """,
            (repo, commit),
        )
        self.commit()
        return n

    def upsert_documents(self, repo: str, commit: str, docs: Sequence[ScipDocument]) -> None:
        self.conn.executemany(
            """
            INSERT OR REPLACE INTO documents(
              document_id, repo, commit_hash, relative_path, language, occurrence_count, content
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    d.document_id,
                    repo,
                    commit,
                    d.relative_path,
                    d.language,
                    d.occurrence_count,
                    d.content,
                )
                for d in docs
            ],
        )

    def upsert_symbols(self, symbols: Sequence[SymbolNode]) -> None:
        self.conn.executemany(
            """
            INSERT OR REPLACE INTO symbols(
              symbol_id, display_name, kind, package, enclosing_symbol, language, signature_hash, symbol_fingerprint
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    s.symbol_id,
                    s.display_name,
                    s.kind,
                    s.package,
                    s.enclosing_symbol,
                    s.language,
                    s.signature_hash,
                    s.symbol_fingerprint,
                )
                for s in symbols
            ],
        )

    def insert_occurrences(self, occurrences: Sequence[OccurrenceEdge]) -> None:
        self.conn.executemany(
            """
            INSERT INTO occurrences(
              document_id, symbol_id, range_start_line, range_start_col, range_end_line, range_end_col, role,
              syntax_kind, enclosing_range_start_line, enclosing_range_start_col, enclosing_range_end_line, enclosing_range_end_col
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    o.document_id,
                    o.symbol_id,
                    o.range_start_line,
                    o.range_start_col,
                    o.range_end_line,
                    o.range_end_col,
                    o.role,
                    o.syntax_kind,
                    o.enclosing_range_start_line,
                    o.enclosing_range_start_col,
                    o.enclosing_range_end_line,
                    o.enclosing_range_end_col,
                )
                for o in occurrences
            ],
        )

    def insert_relations(self, relations: Sequence[RelationEdge]) -> None:
        self.conn.executemany(
            """
            INSERT INTO relations(from_symbol, to_symbol, relation_type, confidence, evidence_document_id)
            VALUES (?, ?, ?, ?, ?)
            """,
            [
                (
                    r.from_symbol,
                    r.to_symbol,
                    r.relation_type,
                    r.confidence,
                    r.evidence_document_id,
                )
                for r in relations
            ],
        )

    def upsert_chunks(self, chunks: Sequence[Chunk]) -> None:
        self.conn.executemany(
            """
            INSERT OR REPLACE INTO chunks(
              chunk_id, document_id, content, primary_symbol_ids, span_start_line, span_end_line, embedding_version
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    c.chunk_id,
                    c.document_id,
                    c.content,
                    json.dumps(c.primary_symbol_ids),
                    c.span_start_line,
                    c.span_end_line,
                    c.embedding_version,
                )
                for c in chunks
            ],
        )
        self.conn.executemany(
            "INSERT OR REPLACE INTO chunks_fts(rowid, chunk_id, content) VALUES ((SELECT rowid FROM chunks WHERE chunk_id = ?), ?, ?)",
            [(c.chunk_id, c.chunk_id, c.content) for c in chunks],
        )

    def upsert_embeddings(self, embedding_version: str, vectors: Dict[str, List[float]]) -> None:
        self.conn.executemany(
            """
            INSERT OR REPLACE INTO embeddings(chunk_id, embedding_version, vector_json) VALUES (?, ?, ?)
            """,
            [(chunk_id, embedding_version, json.dumps(vec)) for chunk_id, vec in vectors.items()],
        )

    def commit(self) -> None:
        self.conn.commit()

    def fetch_documents_for_chunking(self, repo: str, commit: str) -> List[sqlite3.Row]:
        cur = self.conn.execute(
            """
            SELECT document_id, relative_path, language, content
            FROM documents WHERE repo = ? AND commit_hash = ? AND content != ''
            """,
            (repo, commit),
        )
        return list(cur.fetchall())

    def fetch_enclosing_symbols_for_ids(self, symbol_ids: Sequence[str]) -> Dict[str, str]:
        """批量返回 symbol_id -> enclosing_symbol（无记录则不在 dict 中）。"""
        ids = [str(x) for x in symbol_ids if x]
        if not ids:
            return {}
        out: Dict[str, str] = {}
        step = 400
        for i in range(0, len(ids), step):
            batch = ids[i : i + step]
            q_marks = ",".join(["?"] * len(batch))
            cur = self.conn.execute(
                f"SELECT symbol_id, enclosing_symbol FROM symbols WHERE symbol_id IN ({q_marks})",
                tuple(batch),
            )
            for row in cur.fetchall():
                out[str(row["symbol_id"])] = str(row["enclosing_symbol"] or "")
        return out

    def fetch_symbol_ids_for_document(self, document_id: str) -> List[str]:
        cur = self.conn.execute(
            "SELECT DISTINCT symbol_id FROM occurrences WHERE document_id = ?",
            (document_id,),
        )
        return [r["symbol_id"] for r in cur.fetchall()]

    @staticmethod
    def _intent_term_score(query: str, text: str, terms: List[str]) -> float:
        text_l = text.lower()
        score = 0.0
        if query.lower() in text_l:
            score += 2.0
        score += float(sum(1 for t in terms if t in text_l))
        return score

    def fetch_definition_occurrences_for_document(self, document_id: str) -> List[sqlite3.Row]:
        signature_expr = "s.display_name AS signature"
        join_code_nodes = ""
        if self._table_exists("code_nodes"):
            signature_expr = "COALESCE(cn.signature, s.display_name) AS signature"
            join_code_nodes = "LEFT JOIN code_nodes cn ON cn.symbol_id = o.symbol_id"
        cur = self.conn.execute(
            f"""
            SELECT o.symbol_id, s.display_name, s.kind, {signature_expr}, o.range_start_line, o.range_end_line
            FROM occurrences o
            JOIN symbols s ON s.symbol_id = o.symbol_id
            {join_code_nodes}
            WHERE o.document_id = ? AND o.role = 'definition'
            ORDER BY o.range_start_line ASC, o.range_end_line ASC
            """,
            (document_id,),
        )
        return list(cur.fetchall())

    def fetch_definition_nodes_for_document(self, document_id: str) -> List[sqlite3.Row]:
        """读取 definition 对应的 SCIP AST 包围范围（优先 enclosing_range）。"""
        signature_expr = "s.display_name AS signature"
        join_code_nodes = ""
        if self._table_exists("code_nodes"):
            signature_expr = "COALESCE(cn.signature, s.display_name) AS signature"
            join_code_nodes = "LEFT JOIN code_nodes cn ON cn.symbol_id = o.symbol_id"
        cur = self.conn.execute(
            f"""
            SELECT
              o.symbol_id,
              s.display_name,
              {signature_expr},
              s.kind,
              o.range_start_line,
              o.range_end_line,
              o.syntax_kind,
              o.enclosing_range_start_line,
              o.enclosing_range_end_line,
              CASE
                WHEN o.enclosing_range_start_line >= 0
                 AND o.enclosing_range_end_line > o.enclosing_range_start_line
                THEN o.enclosing_range_start_line
                ELSE o.range_start_line
              END AS node_start_line,
              CASE
                WHEN o.enclosing_range_start_line >= 0
                 AND o.enclosing_range_end_line > o.enclosing_range_start_line
                THEN o.enclosing_range_end_line
                ELSE o.range_end_line
              END AS node_end_line,
              CASE
                WHEN o.enclosing_range_start_line >= 0
                 AND o.enclosing_range_end_line > o.enclosing_range_start_line
                THEN 1
                ELSE 0
              END AS has_explicit_enclosing_range
            FROM occurrences o
            JOIN symbols s ON s.symbol_id = o.symbol_id
            {join_code_nodes}
            WHERE o.document_id = ? AND o.role = 'definition'
            ORDER BY node_start_line ASC, node_end_line ASC, o.range_start_line ASC
            """,
            (document_id,),
        )
        return list(cur.fetchall())

    def def_of(self, symbol_id: str, top_k: int) -> List[QueryResult]:
        cur = self.conn.execute(
            """
            SELECT o.document_id, d.relative_path, o.range_start_line, o.range_start_col
            FROM occurrences o
            JOIN documents d ON d.document_id = o.document_id
            WHERE o.symbol_id = ? AND o.role = 'definition'
            LIMIT ?
            """,
            (symbol_id, top_k),
        )
        return [
            QueryResult(
                result_id=f"{r['document_id']}:{r['range_start_line']}:{r['range_start_col']}",
                result_type="definition",
                score=1.0,
                explain={"structure": 1.0},
                payload={
                    "symbol_id": symbol_id,
                    "document_id": r["document_id"],
                    "path": r["relative_path"],
                    "start_line": r["range_start_line"],
                    "start_col": r["range_start_col"],
                },
            )
            for r in cur.fetchall()
        ]

    def refs_of(self, symbol_id: str, top_k: int) -> List[QueryResult]:
        cur = self.conn.execute(
            """
            SELECT
              o.document_id,
              d.relative_path,
              COUNT(*) AS cnt,
              SUM(CASE WHEN o.role = 'reference' THEN 1 ELSE 0 END) AS explicit_ref_cnt,
              SUM(CASE WHEN o.role = 'unknown' THEN 1 ELSE 0 END) AS inferred_ref_cnt
            FROM occurrences o
            JOIN documents d ON d.document_id = o.document_id
            WHERE o.symbol_id = ? AND o.role != 'definition'
            GROUP BY o.document_id, d.relative_path
            ORDER BY cnt DESC
            LIMIT ?
            """,
            (symbol_id, top_k),
        )
        return [
            QueryResult(
                result_id=r["document_id"],
                result_type="reference_doc",
                score=float(r["cnt"]),
                explain={"structure": float(r["cnt"])},
                payload={
                    "symbol_id": symbol_id,
                    "document_id": r["document_id"],
                    "path": r["relative_path"],
                    "reference_count": int(r["cnt"]),
                    "explicit_reference_count": int(r["explicit_ref_cnt"] or 0),
                    "inferred_reference_count": int(r["inferred_ref_cnt"] or 0),
                },
            )
            for r in cur.fetchall()
        ]

    def _call_graph_results(self, symbol_id: str, top_k: int, reverse: bool) -> List[QueryResult]:
        if not (self._table_exists("code_nodes") and self._table_exists("code_edges")):
            return []
        join_col = "ce.dst_node" if reverse else "ce.src_node"
        select_col = "src.symbol_id" if reverse else "dst.symbol_id"
        path_col = "src.path" if reverse else "dst.path"
        type_col = "src.node_type" if reverse else "dst.node_type"
        cur = self.conn.execute(
            f"""
            SELECT
              {select_col} AS symbol_id,
              MAX(ce.confidence) AS confidence,
              MAX({type_col}) AS node_type,
              MIN({path_col}) AS path
            FROM code_edges ce
            JOIN code_nodes pivot ON pivot.node_id = {join_col}
            JOIN code_nodes src ON src.node_id = ce.src_node
            JOIN code_nodes dst ON dst.node_id = ce.dst_node
            WHERE pivot.symbol_id = ? AND ce.edge_type = 'calls'
            GROUP BY {select_col}
            ORDER BY confidence DESC, symbol_id ASC
            LIMIT ?
            """,
            (symbol_id, top_k),
        )
        return [
            QueryResult(
                result_id=str(r["symbol_id"]),
                result_type="symbol",
                score=float(r["confidence"]),
                explain={
                    "structure": float(r["confidence"]),
                    "code_edges": float(r["confidence"]),
                },
                payload={
                    "symbol_id": str(r["symbol_id"]),
                    "node_type": str(r["node_type"] or ""),
                    "path": str(r["path"] or ""),
                    "source": "code_edges",
                },
            )
            for r in cur.fetchall()
        ]

    def _relation_call_results(self, symbol_id: str, top_k: int, reverse: bool) -> List[QueryResult]:
        target_col = "to_symbol" if reverse else "from_symbol"
        filter_col = "from_symbol" if reverse else "to_symbol"
        cur = self.conn.execute(
            f"""
            SELECT {target_col} AS symbol_id, MAX(confidence) AS confidence
            FROM relations
            WHERE {filter_col} = ? AND relation_type = 'calls'
            GROUP BY {target_col}
            ORDER BY confidence DESC, symbol_id ASC
            LIMIT ?
            """,
            (symbol_id, top_k),
        )
        return [
            QueryResult(
                result_id=str(r["symbol_id"]),
                result_type="symbol",
                score=float(r["confidence"]),
                explain={
                    "structure": float(r["confidence"]),
                    "relations": float(r["confidence"]),
                },
                payload={
                    "symbol_id": str(r["symbol_id"]),
                    "source": "relations",
                },
            )
            for r in cur.fetchall()
        ]

    def _merge_ranked_results(self, primary: List[QueryResult], fallback: List[QueryResult], top_k: int) -> List[QueryResult]:
        merged: Dict[str, QueryResult] = {}
        for item in [*primary, *fallback]:
            existing = merged.get(item.result_id)
            if existing is None:
                merged[item.result_id] = QueryResult(
                    result_id=item.result_id,
                    result_type=item.result_type,
                    score=item.score,
                    explain=dict(item.explain),
                    payload=dict(item.payload or {}) or None,
                )
                continue
            existing.score = max(existing.score, item.score)
            existing.explain.update(item.explain)
            if item.payload:
                base_payload = dict(existing.payload or {})
                for key, value in item.payload.items():
                    if key not in base_payload or base_payload[key] in {"", None}:
                        base_payload[key] = value
                existing.payload = base_payload
        ranked = list(merged.values())
        ranked.sort(key=lambda x: (x.score, x.result_id), reverse=True)
        return ranked[:top_k]

    def callers_of(self, symbol_id: str, top_k: int) -> List[QueryResult]:
        graph_results = self._call_graph_results(symbol_id, top_k, reverse=True)
        relation_results = self._relation_call_results(symbol_id, top_k, reverse=False)
        return self._merge_ranked_results(graph_results, relation_results, top_k)

    def callees_of(self, symbol_id: str, top_k: int) -> List[QueryResult]:
        graph_results = self._call_graph_results(symbol_id, top_k, reverse=False)
        relation_results = self._relation_call_results(symbol_id, top_k, reverse=True)
        return self._merge_ranked_results(graph_results, relation_results, top_k)

    def symbol_exact(self, query: str, top_k: int) -> List[QueryResult]:
        """按 query 词项在「display_name + symbol_id」上的命中数打分。

        symbol_id 含 maven 坐标与源码路径（如 io/netty/buffer/AbstractByteBuf#readBytes），
        便于用「类名、包路径、方法名」等组合缩小候选，避免仅靠 display_name（常为短方法名）时大量撞名。
        """
        query_terms = [t.strip().lower() for t in query.split() if t.strip()]
        ql = query.lower().strip()
        cur = self.conn.execute("SELECT symbol_id, display_name FROM symbols")
        scored = []
        for r in cur.fetchall():
            name = r["display_name"].lower()
            sid = r["symbol_id"].lower()
            haystack = f"{name} {sid}"
            if ql in haystack:
                score = 2.0
            else:
                score = float(sum(1 for t in query_terms if t in haystack))
            if score <= 0:
                continue
            scored.append(
                QueryResult(
                    result_id=r["symbol_id"],
                    result_type="symbol",
                    score=score,
                    explain={"symbol_exact": score},
                    payload={"display_name": r["display_name"]},
                )
            )
        scored.sort(key=lambda x: x.score, reverse=True)
        return scored[:top_k]

    def find_entities(
        self,
        entity_type: str,
        name: str,
        *,
        match: str = "contains",
        package_contains: str = "",
        limit: int = 50,
    ) -> List[EntityHit]:
        """在符号表上做实体级查询（类 / 方法 / 字段等），见 :func:`entity_query.find_entity`。"""
        from .entity_query import find_entity

        return find_entity(
            self,
            type=entity_type,
            name=name,
            match=match,  # type: ignore[arg-type]
            package_contains=package_contains,
            limit=limit,
        )

    def search_function_intents(self, query: str, top_k: int) -> List[Dict[str, object]]:
        terms = [t for t in re.split(r"[^A-Za-z0-9_]+", query.lower()) if t]
        cur = self.conn.execute(
            """
            SELECT fi.node_id, fi.intent_text, fi.role_in_chain, fi.quality_score, cn.path, cn.signature
            FROM function_intents fi
            JOIN code_nodes cn ON cn.node_id = fi.node_id
            """
        )
        hits: List[Dict[str, object]] = []
        for r in cur.fetchall():
            text = str(r["intent_text"])
            score = self._intent_term_score(query, text, terms)
            if score <= 0:
                continue
            hits.append(
                {
                    "node_id": r["node_id"],
                    "intent_text": text,
                    "role": r["role_in_chain"],
                    "quality_score": float(r["quality_score"]),
                    "path": r["path"],
                    "signature": r["signature"],
                    "score": score,
                    "source": "function",
                }
            )
        hits.sort(key=lambda x: (float(x["score"]), float(x["quality_score"])), reverse=True)
        return hits[:top_k]

    def search_module_intents(self, query: str, top_k: int) -> List[Dict[str, object]]:
        terms = [t for t in re.split(r"[^A-Za-z0-9_]+", query.lower()) if t]
        cur = self.conn.execute(
            """
            SELECT community_id, module_intent, module_tags_json, backbone_json, cohesion_score, size
            FROM module_intents
            """
        )
        hits: List[Dict[str, object]] = []
        for r in cur.fetchall():
            text = str(r["module_intent"])
            tags = [str(t) for t in json.loads(r["module_tags_json"] or "[]")]
            backbone_raw = str(r["backbone_json"] or "")
            score = self._intent_term_score(query, text, terms)
            score += 0.5 * float(sum(1 for t in terms if any(t in tag.lower() for tag in tags)))
            score += 0.35 * self._intent_term_score(query, backbone_raw, terms)
            if score <= 0:
                continue
            hits.append(
                {
                    "community_id": r["community_id"],
                    "module_intent": text,
                    "tags": tags,
                    "backbone_json": backbone_raw,
                    "cohesion_score": float(r["cohesion_score"]),
                    "size": int(r["size"]),
                    "score": score,
                    "source": "module",
                }
            )
        hits.sort(key=lambda x: (float(x["score"]), float(x["cohesion_score"])), reverse=True)
        return hits[:top_k]

    def fetch_community_seed_nodes(self, community_id: str, top_k: int) -> List[Dict[str, object]]:
        cur = self.conn.execute(
            """
            SELECT ic.node_id, ic.assign_score, ic.cohesion_score, ic.assignment_mode,
                   cn.signature, cn.path, cn.fan_in, cn.fan_out
            FROM intent_communities ic
            JOIN code_nodes cn ON cn.node_id = ic.node_id
            WHERE ic.community_id = ?
            ORDER BY ic.assign_score DESC, ic.cohesion_score DESC, (cn.fan_in + cn.fan_out) DESC
            LIMIT ?
            """,
            (community_id, top_k),
        )
        return [dict(r) for r in cur.fetchall()]

    def fetch_chunk_primary_symbols(self, chunk_id: str) -> List[str]:
        cur = self.conn.execute(
            """
            SELECT primary_symbol_ids
            FROM chunks
            WHERE chunk_id = ?
            LIMIT 1
            """,
            (chunk_id,),
        )
        row = cur.fetchone()
        if row is None:
            return []
        try:
            vals = json.loads(row["primary_symbol_ids"] or "[]")
            return [str(v) for v in vals]
        except Exception:
            return []

    def keyword_search(self, query: str, top_k: int) -> List[QueryResult]:
        terms = [t for t in re.split(r"[^A-Za-z0-9_]+", query) if t]
        if not terms:
            return []
        fts_query = " OR ".join(f'"{t}"' for t in terms)
        cur = self.conn.execute(
            """
            SELECT c.chunk_id, c.document_id, d.relative_path, bm25(chunks_fts) AS score
            FROM chunks_fts
            JOIN chunks c ON c.rowid = chunks_fts.rowid
            JOIN documents d ON d.document_id = c.document_id
            WHERE chunks_fts MATCH ?
            ORDER BY score
            LIMIT ?
            """,
            (fts_query, top_k),
        )
        rows = cur.fetchall()
        return [
            QueryResult(
                result_id=r["chunk_id"],
                result_type="chunk",
                score=float(-r["score"]),
                explain={"keyword": float(-r["score"])},
                payload={"path": r["relative_path"], "document_id": r["document_id"]},
            )
            for r in rows
        ]

    def fetch_chunks(self) -> List[sqlite3.Row]:
        cur = self.conn.execute("SELECT chunk_id, content FROM chunks")
        return list(cur.fetchall())

    def count_chunks(self) -> int:
        cur = self.conn.execute("SELECT COUNT(*) AS cnt FROM chunks")
        row = cur.fetchone()
        return int(row["cnt"]) if row is not None else 0

    def fetch_chunks_missing_embeddings(self, embedding_version: str) -> List[sqlite3.Row]:
        cur = self.conn.execute(
            """
            SELECT c.chunk_id, c.content
            FROM chunks c
            LEFT JOIN embeddings e
              ON e.chunk_id = c.chunk_id
             AND e.embedding_version = ?
            WHERE e.chunk_id IS NULL
            ORDER BY c.chunk_id
            """,
            (embedding_version,),
        )
        return list(cur.fetchall())

    def count_chunks_missing_embeddings(self, embedding_version: str) -> int:
        cur = self.conn.execute(
            """
            SELECT COUNT(*) AS cnt
            FROM chunks c
            LEFT JOIN embeddings e
              ON e.chunk_id = c.chunk_id
             AND e.embedding_version = ?
            WHERE e.chunk_id IS NULL
            """,
            (embedding_version,),
        )
        row = cur.fetchone()
        return int(row["cnt"]) if row is not None else 0

    def fetch_chunks_missing_embeddings_page(
        self,
        embedding_version: str,
        after_chunk_id: str | None,
        limit: int,
    ) -> List[sqlite3.Row]:
        """按 chunk_id 递增分页拉取尚未写入 embeddings 的 chunk，避免一次性加载全表。"""
        limit = max(1, int(limit))
        if after_chunk_id is None:
            cur = self.conn.execute(
                """
                SELECT c.chunk_id, c.content
                FROM chunks c
                LEFT JOIN embeddings e
                  ON e.chunk_id = c.chunk_id
                 AND e.embedding_version = ?
                WHERE e.chunk_id IS NULL
                ORDER BY c.chunk_id
                LIMIT ?
                """,
                (embedding_version, limit),
            )
        else:
            cur = self.conn.execute(
                """
                SELECT c.chunk_id, c.content
                FROM chunks c
                LEFT JOIN embeddings e
                  ON e.chunk_id = c.chunk_id
                 AND e.embedding_version = ?
                WHERE e.chunk_id IS NULL
                  AND c.chunk_id > ?
                ORDER BY c.chunk_id
                LIMIT ?
                """,
                (embedding_version, after_chunk_id, limit),
            )
        return list(cur.fetchall())

    def fetch_embeddings(self, embedding_version: str) -> Dict[str, List[float]]:
        cur = self.conn.execute(
            "SELECT chunk_id, vector_json FROM embeddings WHERE embedding_version = ?",
            (embedding_version,),
        )
        return {r["chunk_id"]: json.loads(r["vector_json"]) for r in cur.fetchall()}

    def fetch_chunk_metadata(self, chunk_id: str, include_content: bool = False) -> Dict[str, object] | None:
        fields = (
            "c.chunk_id, c.document_id, c.span_start_line, c.span_end_line, d.relative_path, d.language"
            + (", c.content" if include_content else "")
        )
        cur = self.conn.execute(
            f"""
            SELECT {fields}
            FROM chunks c
            JOIN documents d ON d.document_id = c.document_id
            WHERE c.chunk_id = ?
            LIMIT 1
            """,
            (chunk_id,),
        )
        row = cur.fetchone()
        if row is None:
            return None
        result: Dict[str, object] = {
            "chunk_id": row["chunk_id"],
            "document_id": row["document_id"],
            "path": row["relative_path"],
            "language": row["language"],
            "start_line": row["span_start_line"],
            "end_line": row["span_end_line"],
        }
        if include_content:
            result["content"] = row["content"]
        return result

    def fetch_relative_path_for_symbol(self, symbol_id: str) -> str | None:
        """取符号任一 definition 所在文档路径（轻量，用于检索侧启发式）。"""
        cur = self.conn.execute(
            """
            SELECT d.relative_path
            FROM occurrences o
            JOIN documents d ON d.document_id = o.document_id
            WHERE o.symbol_id = ? AND o.role = 'definition'
            ORDER BY o.range_start_line ASC
            LIMIT 1
            """,
            (symbol_id,),
        )
        row = cur.fetchone()
        if row is None:
            return None
        return str(row["relative_path"] or "") or None

    def fetch_symbol_definition_snippet(self, symbol_id: str) -> Dict[str, object] | None:
        cur = self.conn.execute(
            """
            SELECT d.relative_path, d.language, d.content, o.range_start_line, o.range_end_line
            FROM occurrences o
            JOIN documents d ON d.document_id = o.document_id
            WHERE o.symbol_id = ? AND o.role = 'definition'
            ORDER BY o.range_start_line ASC
            LIMIT 1
            """,
            (symbol_id,),
        )
        row = cur.fetchone()
        if row is None:
            return None
        content = row["content"] or ""
        if not content:
            return {
                "path": row["relative_path"],
                "language": row["language"],
                "start_line": row["range_start_line"],
                "end_line": row["range_end_line"],
                "code": "",
            }
        lines = content.splitlines()
        start_line = int(row["range_start_line"])
        end_line = max(start_line + 1, int(row["range_end_line"]))
        lo = max(0, start_line - 8)
        hi = min(len(lines), end_line + 8)
        snippet = "\n".join(lines[lo:hi])
        return {
            "path": row["relative_path"],
            "language": row["language"],
            "start_line": lo,
            "end_line": hi,
            "code": snippet,
        }
