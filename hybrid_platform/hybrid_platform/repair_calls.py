from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Dict, List, Tuple

from .embedding import cosine
from .isolated_policy import IsolatedNodePolicy
from .storage import SqliteStore


@dataclass
class RepairCallsStats:
    missing_nodes: int = 0
    inserted_edges: int = 0
    skipped_nodes: int = 0
    reclassified: bool = False


def _edge_id(src_node: str, dst_node: str) -> str:
    return f"repair:{src_node}->{dst_node}"


class CallsRepairer:
    def __init__(self, store: SqliteStore) -> None:
        self.store = store

    @staticmethod
    def _path_prior(path_a: str, path_b: str) -> float:
        a = path_a.split("/")
        b = path_b.split("/")
        common = 0
        for x, y in zip(a, b):
            if x != y:
                break
            common += 1
        return min(1.0, common / 4.0)

    def _load_vectors(self) -> Dict[str, Tuple[List[float], str]]:
        cur = self.store.conn.execute(
            """
            SELECT fi.node_id, fi.semantic_vec_json, cn.path
            FROM function_intents fi
            JOIN code_nodes cn ON cn.node_id = fi.node_id
            """
        )
        return {r["node_id"]: (json.loads(r["semantic_vec_json"]), r["path"]) for r in cur.fetchall()}

    def run(
        self,
        top_k: int = 6,
        sim_threshold: float = 0.58,
        max_edges_per_node: int = 3,
        reclassify: bool = True,
    ) -> RepairCallsStats:
        stats = RepairCallsStats()
        vecs = self._load_vectors()
        missing_rows = self.store.conn.execute(
            """
            SELECT node_id
            FROM code_nodes
            WHERE isolated_type = 'MissingEdge'
            """
        ).fetchall()
        missing_ids = [r["node_id"] for r in missing_rows]
        stats.missing_nodes = len(missing_ids)
        for src in missing_ids:
            if src not in vecs:
                stats.skipped_nodes += 1
                continue
            src_vec, src_path = vecs[src]
            candidates = []
            for dst, (dst_vec, dst_path) in vecs.items():
                if dst == src:
                    continue
                sem = cosine(src_vec, dst_vec)
                score = 0.85 * max(0.0, sem) + 0.15 * self._path_prior(src_path, dst_path)
                if score >= sim_threshold:
                    candidates.append((dst, score))
            candidates.sort(key=lambda x: x[1], reverse=True)
            candidates = candidates[:top_k]
            inserted_for_src = 0
            for dst, score in candidates:
                if inserted_for_src >= max_edges_per_node:
                    break
                confidence = min(0.69, 0.25 + score * 0.4)
                self.store.conn.execute(
                    """
                    INSERT OR IGNORE INTO code_edges(
                      edge_id, src_node, dst_node, edge_type, weight, confidence, evidence_json
                    ) VALUES (?, ?, ?, 'calls', ?, ?, ?)
                    """,
                    (
                        _edge_id(src, dst),
                        src,
                        dst,
                        confidence,
                        confidence,
                        json.dumps({"source": "repair_missing_edge"}),
                    ),
                )
                self.store.conn.execute(
                    """
                    INSERT OR IGNORE INTO code_edges(
                      edge_id, src_node, dst_node, edge_type, weight, confidence, evidence_json
                    ) VALUES (?, ?, ?, 'calls', ?, ?, ?)
                    """,
                    (
                        _edge_id(dst, src),
                        dst,
                        src,
                        confidence * 0.9,
                        confidence * 0.9,
                        json.dumps({"source": "repair_missing_edge_reverse"}),
                    ),
                )
                inserted_for_src += 2
                stats.inserted_edges += 2

        self.store.conn.execute("UPDATE code_nodes SET fan_in = 0, fan_out = 0")
        self.store.conn.execute(
            """
            UPDATE code_nodes
            SET fan_out = (
              SELECT COUNT(*) FROM code_edges e
              WHERE e.src_node = code_nodes.node_id AND e.edge_type = 'calls'
            )
            """
        )
        self.store.conn.execute(
            """
            UPDATE code_nodes
            SET fan_in = (
              SELECT COUNT(*) FROM code_edges e
              WHERE e.dst_node = code_nodes.node_id AND e.edge_type = 'calls'
            )
            """
        )
        self.store.conn.commit()

        if reclassify:
            IsolatedNodePolicy(self.store).run()
            stats.reclassified = True
        return stats
