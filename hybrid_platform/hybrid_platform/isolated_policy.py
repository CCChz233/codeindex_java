from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Dict, List

from .embedding import cosine
from .storage import SqliteStore


@dataclass
class IsolatedPolicyStats:
    total_nodes: int = 0
    isolated_nodes: int = 0
    uncertain_nodes: int = 0
    reassigned_nodes: int = 0


class IsolatedNodePolicy:
    def __init__(
        self,
        store: SqliteStore,
        force_threshold_default: float = 0.55,
        force_threshold_uncertain: float = 0.65,
        force_threshold_entrypoint: float = 0.60,
    ) -> None:
        self.store = store
        self.force_threshold_default = force_threshold_default
        self.force_threshold_uncertain = force_threshold_uncertain
        self.force_threshold_entrypoint = force_threshold_entrypoint

    def _semantic_knn_sim(self, node_id: str, vec: List[float], all_vecs: Dict[str, List[float]], k: int = 5) -> float:
        sims = []
        for other_id, other_vec in all_vecs.items():
            if other_id == node_id:
                continue
            sims.append(cosine(vec, other_vec))
        if not sims:
            return 0.0
        sims.sort(reverse=True)
        top = sims[:k]
        return float(sum(top) / max(1, len(top)))

    def _classify(self, row: Dict[str, object], semantic_knn_sim: float) -> tuple[str, float, Dict[str, object]]:
        fan_in = int(row["fan_in"])
        fan_out = int(row["fan_out"])
        path = str(row["path"])
        intra_repo_ratio = 1.0
        score: Dict[str, float] = {
            "BoundaryExternal": (0.7 if "third_party/" in path else 0.0) + (0.3 if intra_repo_ratio < 0.3 else 0.0),
            "Entrypoint": (1.0 if fan_in == 0 and fan_out >= 3 else 0.0),
            "TrueLeaf": (1.0 if fan_out == 0 and fan_in >= 1 else 0.0) + (0.2 if semantic_knn_sim >= 0.5 else 0.0),
            "MissingEdge": (1.0 if fan_in == 0 and fan_out == 0 and semantic_knn_sim >= 0.65 else 0.0),
            "NoiseNode": (1.0 if fan_in == 0 and fan_out == 0 and semantic_knn_sim < 0.35 else 0.0),
        }
        best = sorted(score.items(), key=lambda x: x[1], reverse=True)
        best_type, best_score = best[0]
        second_score = best[1][1] if len(best) > 1 else 0.0
        uncertain = best_score < 0.5 or (best_score - second_score) < 0.1
        if uncertain:
            best_type = "Uncertain"
            best_score = max(best_score, 0.45)
        reason = {
            "fan_in": fan_in,
            "fan_out": fan_out,
            "semantic_knn_sim": semantic_knn_sim,
            "raw_scores": score,
        }
        return best_type, float(min(1.0, best_score)), reason

    def _assign_singletons(self) -> int:
        cur = self.store.conn.execute(
            """
            SELECT n.node_id, n.path, n.isolated_type, fi.semantic_vec_json
            FROM code_nodes n
            JOIN function_intents fi ON fi.node_id = n.node_id
            WHERE n.is_isolated = 1
            """
        )
        isolated = cur.fetchall()
        comm_rows = self.store.conn.execute(
            """
            SELECT ic.community_id, ic.node_id, fi.semantic_vec_json, cn.path
            FROM intent_communities ic
            JOIN function_intents fi ON fi.node_id = ic.node_id
            JOIN code_nodes cn ON cn.node_id = ic.node_id
            """
        ).fetchall()
        by_comm: Dict[str, List[List[float]]] = {}
        by_comm_paths: Dict[str, List[str]] = {}
        for r in comm_rows:
            by_comm.setdefault(r["community_id"], []).append(json.loads(r["semantic_vec_json"]))
            by_comm_paths.setdefault(r["community_id"], []).append(r["path"])

        # ExternalBoundary 虚拟社区
        self.store.conn.execute(
            """
            INSERT OR IGNORE INTO module_intents(
              community_id, module_intent, module_tags_json, backbone_json, cohesion_score, size
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                "intent:community:ExternalBoundary",
                "External boundary functions and third-party integrations.",
                json.dumps(["external", "boundary"]),
                json.dumps({}),
                0.0,
                0,
            ),
        )

        def path_prior(path_a: str, path_b: str) -> float:
            a = path_a.split("/")
            b = path_b.split("/")
            common = 0
            for x, y in zip(a, b):
                if x != y:
                    break
                common += 1
            return min(1.0, common / 4.0)

        def topo_similarity(node_id: str, community_id: str) -> float:
            members = self.store.conn.execute(
                "SELECT node_id FROM intent_communities WHERE community_id = ? LIMIT 200",
                (community_id,),
            ).fetchall()
            if not members:
                return 0.0
            member_ids = {m["node_id"] for m in members}
            q_marks = ",".join(["?"] * len(member_ids))
            if not q_marks:
                return 0.0
            cur_hits = self.store.conn.execute(
                f"""
                SELECT COUNT(*) AS c
                FROM code_edges
                WHERE edge_type = 'calls'
                  AND (
                    (src_node = ? AND dst_node IN ({q_marks}))
                    OR
                    (dst_node = ? AND src_node IN ({q_marks}))
                  )
                """,
                (node_id, *member_ids, node_id, *member_ids),
            ).fetchone()["c"]
            deg = self.store.conn.execute(
                """
                SELECT (fan_in + fan_out) AS d
                FROM code_nodes
                WHERE node_id = ?
                """,
                (node_id,),
            ).fetchone()["d"]
            return float(cur_hits / max(1, deg))

        reassigned = 0
        for row in isolated:
            node_id = row["node_id"]
            if row["semantic_vec_json"] is None:
                continue
            vec = json.loads(row["semantic_vec_json"])
            node_path = row["path"]
            node_type = row["isolated_type"]

            if node_type == "BoundaryExternal":
                self.store.conn.execute(
                    """
                    INSERT OR REPLACE INTO intent_communities(community_id, node_id, cohesion_score, assign_score, assignment_mode)
                    VALUES (?, ?, 0.0, 1.0, 'external_boundary')
                    """,
                    ("intent:community:ExternalBoundary", node_id),
                )
                reassigned += 1
                continue

            if node_type in {"TrueLeaf", "NoiseNode"}:
                continue

            best_comm = None
            best_score = 0.0
            for community_id, vectors in by_comm.items():
                if not vectors:
                    continue
                sims = [cosine(vec, cvec) for cvec in vectors]
                sem_score = sum(sims) / max(1, len(sims))
                topo_score = topo_similarity(node_id, community_id)
                paths = by_comm_paths.get(community_id, [])
                path_score = max([path_prior(node_path, p) for p in paths], default=0.0)
                score = 0.6 * sem_score + 0.3 * topo_score + 0.1 * path_score
                if score > best_score:
                    best_score = float(score)
                    best_comm = community_id
            threshold = self.force_threshold_default
            if node_type == "Uncertain":
                threshold = self.force_threshold_uncertain
            elif node_type == "Entrypoint":
                threshold = self.force_threshold_entrypoint
            if best_comm and best_score >= threshold:
                self.store.conn.execute(
                    """
                    INSERT OR REPLACE INTO intent_communities(community_id, node_id, cohesion_score, assign_score, assignment_mode)
                    VALUES (?, ?, 0.0, ?, 'forced')
                    """,
                    (best_comm, node_id, best_score),
                )
                reassigned += 1
        return reassigned

    def run(self) -> IsolatedPolicyStats:
        stats = IsolatedPolicyStats()
        cur = self.store.conn.execute(
            """
            SELECT n.node_id, n.path, n.fan_in, n.fan_out, fi.semantic_vec_json
            FROM code_nodes n
            LEFT JOIN function_intents fi ON fi.node_id = n.node_id
            """
        )
        rows = cur.fetchall()
        stats.total_nodes = len(rows)
        vectors = {
            r["node_id"]: json.loads(r["semantic_vec_json"])
            for r in rows
            if r["semantic_vec_json"] is not None
        }
        for r in rows:
            node_id = r["node_id"]
            fan_in = int(r["fan_in"])
            fan_out = int(r["fan_out"])
            is_isolated = 1 if (fan_in + fan_out) == 0 else 0
            if not is_isolated:
                self.store.conn.execute(
                    """
                    UPDATE code_nodes
                    SET is_isolated = 0, isolated_type = '', isolation_confidence = 0, isolation_reason = '{}'
                    WHERE node_id = ?
                    """,
                    (node_id,),
                )
                continue
            sem = 0.0
            if node_id in vectors:
                sem = self._semantic_knn_sim(node_id, vectors[node_id], vectors, k=5)
            isolated_type, conf, reason = self._classify(dict(r), sem)
            stats.isolated_nodes += 1
            if isolated_type == "Uncertain":
                stats.uncertain_nodes += 1
            self.store.conn.execute(
                """
                UPDATE code_nodes
                SET is_isolated = ?, isolated_type = ?, isolation_confidence = ?, isolation_reason = ?
                WHERE node_id = ?
                """,
                (is_isolated, isolated_type, conf, json.dumps(reason), node_id),
            )
        stats.reassigned_nodes = self._assign_singletons()
        self.store.conn.commit()
        return stats
