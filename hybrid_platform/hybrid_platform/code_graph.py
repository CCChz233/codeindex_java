from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Dict, List

from .storage import SqliteStore


GRAPH_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS code_nodes (
  node_id TEXT PRIMARY KEY,
  symbol_id TEXT UNIQUE NOT NULL,
  node_type TEXT NOT NULL,
  path TEXT NOT NULL,
  signature TEXT NOT NULL,
  fan_in INTEGER NOT NULL DEFAULT 0,
  fan_out INTEGER NOT NULL DEFAULT 0,
  is_isolated INTEGER NOT NULL DEFAULT 0,
  isolated_type TEXT NOT NULL DEFAULT '',
  isolation_confidence REAL NOT NULL DEFAULT 0,
  isolation_reason TEXT NOT NULL DEFAULT '{}',
  meta_json TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_code_nodes_symbol ON code_nodes(symbol_id);
CREATE INDEX IF NOT EXISTS idx_code_nodes_path ON code_nodes(path);

CREATE TABLE IF NOT EXISTS code_edges (
  edge_id TEXT PRIMARY KEY,
  src_node TEXT NOT NULL,
  dst_node TEXT NOT NULL,
  edge_type TEXT NOT NULL,
  weight REAL NOT NULL,
  confidence REAL NOT NULL,
  evidence_json TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_code_edges_src ON code_edges(src_node);
CREATE INDEX IF NOT EXISTS idx_code_edges_dst ON code_edges(dst_node);
"""


@dataclass
class CodeGraphStats:
    nodes: int = 0
    edges: int = 0
    direct_call_edges: int = 0
    inferred_call_edges: int = 0
    direct_call_edges_added: int = 0
    inferred_call_edges_added: int = 0


def _edge_id(src_node: str, dst_node: str, edge_type: str) -> str:
    payload = f"{src_node}|{dst_node}|{edge_type}".encode("utf-8")
    return hashlib.sha1(payload).hexdigest()


def _coarse_node_type(kind: str) -> str:
    value = (kind or "").lower()
    if "package" in value or value == "module":
        return "package"
    if "interface" in value:
        return "interface"
    if "constructor" in value:
        return "method"
    if "method" in value or "function" in value:
        return "method"
    if "field" in value or "property" in value or "constant" in value:
        return "field"
    if "class" in value or "enum" in value or "struct" in value or "object" in value or "type" in value:
        return "class"
    return "symbol"


def _node_id(symbol_id: str, node_type: str) -> str:
    if node_type == "package" and symbol_id.startswith("pkg:"):
        return symbol_id
    prefix = {
        "package": "pkg",
        "class": "type",
        "interface": "type",
        "method": "method",
        "field": "field",
    }.get(node_type, "sym")
    return f"{prefix}:{symbol_id}"


def _range_contains(outer: Dict[str, int], inner: Dict[str, int]) -> bool:
    if outer["start_line"] > inner["start_line"]:
        return False
    if outer["end_line"] < inner["end_line"]:
        return False
    if outer["start_line"] == inner["start_line"] and outer["start_col"] > inner["start_col"]:
        return False
    if outer["end_line"] == inner["end_line"] and outer["end_col"] < inner["end_col"]:
        return False
    return True


def _point_in_range(point_line: int, point_col: int, r: Dict[str, int]) -> bool:
    """Check if (point_line, point_col) is inside range r (inclusive bounds)."""
    if r["start_line"] > point_line or r["end_line"] < point_line:
        return False
    if r["start_line"] == point_line and r["start_col"] > point_col:
        return False
    if r["end_line"] == point_line and r["end_col"] < point_col:
        return False
    return True


def _span_size(r: Dict[str, int]) -> int:
    """Span size for choosing innermost scope when multiple intervals overlap."""
    return (r["end_line"] - r["start_line"]) * 10**6 + (r["end_col"] - r["start_col"])


def _expand_def_spans(items: List[Dict[str, object]], max_end_line: int) -> List[Dict[str, object]]:
    if not items:
        return []
    ordered = sorted(
        items,
        key=lambda x: (
            int(x["start_line"]),
            int(x["start_col"]),
            int(x["end_line"]),
            int(x["end_col"]),
        ),
    )
    out: List[Dict[str, object]] = []
    for idx, item in enumerate(ordered):
        expanded = dict(item)
        if idx + 1 < len(ordered):
            next_start = int(ordered[idx + 1]["start_line"])
            expanded["end_line"] = max(int(expanded["end_line"]), max(int(expanded["start_line"]), next_start - 1))
            expanded["end_col"] = 10**9
        else:
            expanded["end_line"] = max(int(expanded["end_line"]), max_end_line)
            expanded["end_col"] = 10**9
        out.append(expanded)
    return out


class CodeGraphBuilder:
    def __init__(self, store: SqliteStore) -> None:
        self.store = store
        self.store.conn.executescript(GRAPH_SCHEMA_SQL)
        self.store.conn.commit()

    def _symbol_meta(self, symbol_id: str) -> Dict[str, object] | None:
        cur = self.store.conn.execute(
            """
            SELECT symbol_id, display_name, kind, enclosing_symbol, language
            FROM symbols
            WHERE symbol_id = ?
            LIMIT 1
            """,
            (symbol_id,),
        )
        row = cur.fetchone()
        return dict(row) if row is not None else None

    def _clear_repo_graph(self, repo: str, commit: str) -> None:
        cur = self.store.conn.execute(
            """
            SELECT node_id
            FROM code_nodes
            WHERE meta_json LIKE ? AND meta_json LIKE ?
            """,
            (f'%"repo": "{repo}"%', f'%"commit": "{commit}"%'),
        )
        node_ids = [str(row["node_id"]) for row in cur.fetchall()]
        if not node_ids:
            return
        # SQLite variable count is capped (often 999); edge delete binds 2*N vars.
        batch_size = 400
        for i in range(0, len(node_ids), batch_size):
            batch = node_ids[i : i + batch_size]
            q_marks = ",".join(["?"] * len(batch))
            self.store.conn.execute(
                f"DELETE FROM code_edges WHERE src_node IN ({q_marks}) OR dst_node IN ({q_marks})",
                tuple(batch + batch),
            )
        for i in range(0, len(node_ids), batch_size):
            batch = node_ids[i : i + batch_size]
            q_marks = ",".join(["?"] * len(batch))
            self.store.conn.execute(
                f"DELETE FROM code_nodes WHERE node_id IN ({q_marks})",
                tuple(batch),
            )

    def _upsert_nodes(self, repo: str, commit: str) -> int:
        cur = self.store.conn.execute(
            """
            SELECT s.symbol_id, s.display_name, s.kind, s.package, s.enclosing_symbol, s.language, d.relative_path
            FROM symbols s
            JOIN occurrences o ON o.symbol_id = s.symbol_id AND o.role = 'definition'
            JOIN documents d ON d.document_id = o.document_id
            WHERE d.repo = ? AND d.commit_hash = ?
            ORDER BY d.relative_path ASC, o.range_start_line ASC, o.range_start_col ASC
            """,
            (repo, commit),
        )
        rows = cur.fetchall()
        by_symbol: Dict[str, Dict[str, object]] = {}
        for row in rows:
            symbol_id = str(row["symbol_id"])
            by_symbol.setdefault(
                symbol_id,
                {
                    "symbol_id": symbol_id,
                    "display_name": str(row["display_name"]),
                    "kind": str(row["kind"]),
                    "package": str(row["package"] or ""),
                    "enclosing_symbol": str(row["enclosing_symbol"] or ""),
                    "language": str(row["language"] or ""),
                    "path": str(row["relative_path"]),
                },
            )

        payload: List[tuple[object, ...]] = []
        created: Dict[str, str] = {}

        def ensure_symbol(symbol_id: str, fallback_path: str) -> None:
            if not symbol_id or symbol_id in created:
                return
            meta = by_symbol.get(symbol_id)
            if meta is None:
                fetched = self._symbol_meta(symbol_id)
                if fetched is None:
                    return
                meta = {
                    "symbol_id": symbol_id,
                    "display_name": str(fetched["display_name"]),
                    "kind": str(fetched["kind"]),
                    "enclosing_symbol": str(fetched["enclosing_symbol"] or ""),
                    "language": str(fetched["language"] or ""),
                    "path": fallback_path,
                }
                by_symbol[symbol_id] = meta
            node_type = _coarse_node_type(str(meta["kind"]))
            if node_type == "symbol":
                return
            owner = str(meta["enclosing_symbol"] or "")
            package_path = str(meta.get("package") or "").strip("/")
            if not owner and node_type in {"class", "interface"} and package_path:
                package_symbol = f"pkg:{package_path}"
                pkg_node_id = _node_id(package_symbol, "package")
                if package_symbol not in created:
                    created[package_symbol] = pkg_node_id
                    payload.append(
                        (
                            pkg_node_id,
                            package_symbol,
                            "package",
                            str(meta.get("path") or fallback_path),
                            package_path.replace("/", "."),
                            json.dumps(
                                {
                                    "repo": repo,
                                    "commit": commit,
                                    "symbol_kind": "Package",
                                    "enclosing_symbol": "",
                                    "language": str(meta.get("language") or ""),
                                }
                            ),
                        )
                    )
            if owner:
                ensure_symbol(owner, str(meta.get("path") or fallback_path))
            node = _node_id(symbol_id, node_type)
            created[symbol_id] = node
            payload.append(
                (
                    node,
                    symbol_id,
                    node_type,
                    str(meta.get("path") or fallback_path),
                    str(meta["display_name"]),
                    json.dumps(
                        {
                            "repo": repo,
                            "commit": commit,
                            "symbol_kind": str(meta["kind"]),
                            "enclosing_symbol": owner,
                            "language": str(meta.get("language") or ""),
                        }
                    ),
                )
            )

        for symbol_id, meta in by_symbol.items():
            ensure_symbol(symbol_id, str(meta["path"]))

        self.store.conn.executemany(
            """
            INSERT OR REPLACE INTO code_nodes(
              node_id, symbol_id, node_type, path, signature, meta_json
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            payload,
        )
        return len(payload)

    def _insert_owner_edges(self, repo: str, commit: str) -> int:
        cur = self.store.conn.execute(
            """
            SELECT child.node_id AS child_node, parent.node_id AS parent_node, child.symbol_id AS child_symbol
            FROM code_nodes child
            JOIN symbols s ON s.symbol_id = child.symbol_id
            JOIN code_nodes parent ON parent.symbol_id = s.enclosing_symbol
            WHERE child.meta_json LIKE ? AND child.meta_json LIKE ?
            """,
            (f'%"repo": "{repo}"%', f'%"commit": "{commit}"%'),
        )
        payload = [
            (
                _edge_id(str(row["child_node"]), str(row["parent_node"]), "belongs_to"),
                str(row["child_node"]),
                str(row["parent_node"]),
                "belongs_to",
                1.0,
                1.0,
                json.dumps({"source": "symbol_owner", "symbol_id": str(row["child_symbol"])}),
            )
            for row in cur.fetchall()
        ]
        cur = self.store.conn.execute(
            """
            SELECT child.node_id AS child_node, child.symbol_id AS child_symbol, s.package
            FROM code_nodes child
            JOIN symbols s ON s.symbol_id = child.symbol_id
            WHERE child.meta_json LIKE ? AND child.meta_json LIKE ?
              AND child.node_type IN ('class', 'interface')
              AND (s.enclosing_symbol = '' OR s.enclosing_symbol IS NULL)
              AND s.package != ''
            """,
            (f'%"repo": "{repo}"%', f'%"commit": "{commit}"%'),
        )
        for row in cur.fetchall():
            package_path = str(row["package"]).strip("/")
            parent_symbol = f"pkg:{package_path}"
            parent_node = _node_id(parent_symbol, "package")
            payload.append(
                (
                    _edge_id(str(row["child_node"]), parent_node, "belongs_to"),
                    str(row["child_node"]),
                    parent_node,
                    "belongs_to",
                    1.0,
                    1.0,
                    json.dumps({"source": "package_owner", "symbol_id": str(row["child_symbol"])}),
                )
            )
        self.store.conn.executemany(
            """
            INSERT OR REPLACE INTO code_edges(
              edge_id, src_node, dst_node, edge_type, weight, confidence, evidence_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            payload,
        )
        return len(payload)

    def _insert_relation_edges(self) -> int:
        cur = self.store.conn.execute(
            """
            SELECT src.node_id AS src_node, src.node_type AS src_type,
                   dst.node_id AS dst_node, dst.node_type AS dst_type, r.relation_type,
                   MAX(r.confidence) AS conf, MIN(r.evidence_document_id) AS evidence
            FROM relations r
            JOIN code_nodes src ON src.symbol_id = r.from_symbol
            JOIN code_nodes dst ON dst.symbol_id = r.to_symbol
            WHERE r.relation_type IN ('calls', 'extends', 'implements')
            GROUP BY src.node_id, dst.node_id, r.relation_type
            """
        )
        payload = [
        ]
        for row in cur.fetchall():
            relation_type = str(row["relation_type"])
            if relation_type == "implements" and str(row["src_type"]) in {"class", "interface"}:
                relation_type = "implements" if str(row["dst_type"]) == "interface" else "extends"
            payload.append(
                (
                    _edge_id(str(row["src_node"]), str(row["dst_node"]), relation_type),
                    str(row["src_node"]),
                    str(row["dst_node"]),
                    relation_type,
                    float(row["conf"]),
                    float(row["conf"]),
                    json.dumps({"source": "relations", "document_id": row["evidence"] or ""}),
                )
            )
        self.store.conn.executemany(
            """
            INSERT OR REPLACE INTO code_edges(
              edge_id, src_node, dst_node, edge_type, weight, confidence, evidence_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            payload,
        )
        return len(payload)

    def _insert_occurrence_edges(self, repo: str, commit: str, max_per_doc: int = 2000) -> int:
        docs = self.store.conn.execute(
            """
            SELECT document_id
            FROM documents
            WHERE repo = ? AND commit_hash = ?
            """,
            (repo, commit),
        ).fetchall()
        inserted = 0
        for row in docs:
            doc_id = str(row["document_id"])
            doc_meta = self.store.conn.execute(
                """
                SELECT content
                FROM documents
                WHERE document_id = ?
                LIMIT 1
                """,
                (doc_id,),
            ).fetchone()
            max_end_line = max(0, len(str(doc_meta["content"] or "").splitlines()) - 1) if doc_meta else 0
            defs = [
                dict(r)
                for r in self.store.conn.execute(
                    """
                    SELECT n.node_id, n.node_type, o.symbol_id,
                           o.range_start_line, o.range_start_col, o.range_end_line, o.range_end_col,
                           o.enclosing_range_start_line, o.enclosing_range_start_col,
                           o.enclosing_range_end_line, o.enclosing_range_end_col
                    FROM occurrences o
                    JOIN code_nodes n ON n.symbol_id = o.symbol_id
                    WHERE o.document_id = ? AND o.role = 'definition'
                    """,
                    (doc_id,),
                ).fetchall()
            ]
            refs = [
                dict(r)
                for r in self.store.conn.execute(
                    """
                    SELECT n.node_id, n.node_type, o.symbol_id,
                           o.range_start_line, o.range_start_col, o.range_end_line, o.range_end_col,
                           o.enclosing_range_start_line, o.enclosing_range_start_col
                    FROM occurrences o
                    JOIN code_nodes n ON n.symbol_id = o.symbol_id
                    WHERE o.document_id = ? AND o.role != 'definition'
                    """,
                    (doc_id,),
                ).fetchall()
            ]
            method_defs = []
            type_defs = []
            for item in defs:
                er_sl = int(item.get("enclosing_range_start_line", -1) or -1)
                er_el = int(item.get("enclosing_range_end_line", -1) or -1)
                has_enclosing = er_sl >= 0 and er_el >= er_sl
                if has_enclosing:
                    span = {
                        "start_line": er_sl,
                        "start_col": int(item.get("enclosing_range_start_col", 0) or 0),
                        "end_line": er_el,
                        "end_col": int(item.get("enclosing_range_end_col", 0) or 0),
                    }
                else:
                    span = {
                        "start_line": int(item["range_start_line"]),
                        "start_col": int(item["range_start_col"]),
                        "end_line": int(item["range_end_line"]),
                        "end_col": int(item["range_end_col"]),
                    }
                record = {**item, **span}
                if str(item["node_type"]) == "method":
                    method_defs.append(record)
                elif str(item["node_type"]) in {"class", "interface"}:
                    type_defs.append(record)
            method_defs.sort(key=lambda x: (int(x["start_line"]), int(x["start_col"])))
            type_defs.sort(key=lambda x: (int(x["start_line"]), int(x["start_col"])))
            expanded_method = _expand_def_spans(
                [{"start_line": d["start_line"], "start_col": d["start_col"], "end_line": d["end_line"], "end_col": d["end_col"]} for d in method_defs
            ],
                max_end_line,
            )
            expanded_type = _expand_def_spans(
                [{"start_line": d["start_line"], "start_col": d["start_col"], "end_line": d["end_line"], "end_col": d["end_col"]} for d in type_defs
            ],
                max_end_line,
            )
            for i, d in enumerate(method_defs):
                er_sl = int(d.get("enclosing_range_start_line", -1) or -1)
                er_el = int(d.get("enclosing_range_end_line", -1) or -1)
                if er_sl < 0 or er_el < er_sl:
                    d["start_line"] = expanded_method[i]["start_line"]
                    d["start_col"] = expanded_method[i]["start_col"]
                    d["end_line"] = expanded_method[i]["end_line"]
                    d["end_col"] = expanded_method[i]["end_col"]
            for i, d in enumerate(type_defs):
                er_sl = int(d.get("enclosing_range_start_line", -1) or -1)
                er_el = int(d.get("enclosing_range_end_line", -1) or -1)
                if er_sl < 0 or er_el < er_sl:
                    d["start_line"] = expanded_type[i]["start_line"]
                    d["start_col"] = expanded_type[i]["start_col"]
                    d["end_line"] = expanded_type[i]["end_line"]
                    d["end_col"] = expanded_type[i]["end_col"]

            edge_counts: Dict[tuple[str, str, str], int] = {}
            for ref in refs:
                if len(edge_counts) >= max_per_doc:
                    break
                ref_line = int(ref["range_start_line"])
                ref_col = int(ref["range_start_col"])
                containing_methods = [d for d in method_defs if _point_in_range(ref_line, ref_col, d)]
                owner_method = min(containing_methods, key=_span_size) if containing_methods else None
                containing_types = [d for d in type_defs if _point_in_range(ref_line, ref_col, d)]
                owner_type = min(containing_types, key=_span_size) if containing_types else None
                dst_node = str(ref["node_id"])
                dst_type = str(ref["node_type"])
                if dst_type == "method" and owner_method is not None:
                    src_node = str(owner_method["node_id"])
                    if src_node != dst_node:
                        edge_counts[(src_node, dst_node, "calls")] = edge_counts.get((src_node, dst_node, "calls"), 0) + 1
                elif dst_type == "field":
                    src = owner_method or owner_type
                    if src is not None:
                        src_node = str(src["node_id"])
                        if src_node != dst_node:
                            edge_counts[(src_node, dst_node, "field_refs")] = edge_counts.get(
                                (src_node, dst_node, "field_refs"), 0
                            ) + 1
            payload = []
            for (src_node, dst_node, edge_type), cnt in edge_counts.items():
                weight = min(1.0, 0.35 + 0.1 * cnt)
                payload.append(
                    (
                        _edge_id(src_node, dst_node, edge_type),
                        src_node,
                        dst_node,
                        edge_type,
                        weight,
                        min(0.95, weight),
                        json.dumps({"source": "occurrences", "document_id": doc_id, "count": cnt}),
                    )
                )
            before = self.store.conn.total_changes
            self.store.conn.executemany(
                """
                INSERT OR REPLACE INTO code_edges(
                  edge_id, src_node, dst_node, edge_type, weight, confidence, evidence_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                payload,
            )
            inserted += max(0, int(self.store.conn.total_changes - before))
        return inserted

    def _refresh_degrees(self) -> None:
        self.store.conn.execute("UPDATE code_nodes SET fan_in = 0, fan_out = 0")
        self.store.conn.execute(
            """
            UPDATE code_nodes
            SET fan_out = (
              SELECT COUNT(*) FROM code_edges e
              WHERE e.src_node = code_nodes.node_id
            )
            """
        )
        self.store.conn.execute(
            """
            UPDATE code_nodes
            SET fan_in = (
              SELECT COUNT(*) FROM code_edges e
              WHERE e.dst_node = code_nodes.node_id
            )
            """
        )

    def build(self, repo: str, commit: str) -> CodeGraphStats:
        stats = CodeGraphStats()
        self._clear_repo_graph(repo, commit)
        stats.nodes = self._upsert_nodes(repo, commit)
        self._insert_owner_edges(repo, commit)
        stats.direct_call_edges_added = self._insert_relation_edges()
        stats.inferred_call_edges_added = self._insert_occurrence_edges(repo, commit)
        self._refresh_degrees()
        cur = self.store.conn.execute("SELECT COUNT(*) AS c FROM code_edges")
        stats.edges = int(cur.fetchone()["c"])
        cur = self.store.conn.execute(
            """
            SELECT COUNT(*) AS c
            FROM code_edges
            WHERE edge_type = 'calls'
              AND evidence_json LIKE '%"source": "relations"%'
            """
        )
        stats.direct_call_edges = int(cur.fetchone()["c"])
        cur = self.store.conn.execute(
            """
            SELECT COUNT(*) AS c
            FROM code_edges
            WHERE edge_type = 'calls'
              AND evidence_json LIKE '%"source": "occurrences"%'
            """
        )
        stats.inferred_call_edges = int(cur.fetchone()["c"])
        self.store.conn.commit()
        return stats
