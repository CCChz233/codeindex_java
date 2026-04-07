from __future__ import annotations

from dataclasses import dataclass

from .storage import SqliteStore


@dataclass
class GraphMetrics:
    isolated_ratio: float
    uncertain_ratio: float
    forced_assignment_ratio: float
    singleton_communities: int
    communities: int


class GraphEvaluator:
    def __init__(self, store: SqliteStore) -> None:
        self.store = store

    def run(self) -> GraphMetrics:
        total_nodes = self.store.conn.execute("SELECT COUNT(*) AS c FROM code_nodes").fetchone()["c"]
        isolated_nodes = self.store.conn.execute(
            "SELECT COUNT(*) AS c FROM code_nodes WHERE is_isolated = 1"
        ).fetchone()["c"]
        uncertain_nodes = self.store.conn.execute(
            "SELECT COUNT(*) AS c FROM code_nodes WHERE is_isolated = 1 AND isolated_type = 'Uncertain'"
        ).fetchone()["c"]
        forced = self.store.conn.execute(
            "SELECT COUNT(*) AS c FROM intent_communities WHERE assignment_mode = 'forced'"
        ).fetchone()["c"]
        all_assigned = self.store.conn.execute("SELECT COUNT(*) AS c FROM intent_communities").fetchone()["c"]
        communities = self.store.conn.execute(
            "SELECT COUNT(DISTINCT community_id) AS c FROM intent_communities"
        ).fetchone()["c"]
        singletons = self.store.conn.execute(
            """
            SELECT COUNT(*) AS c FROM (
              SELECT community_id, COUNT(*) AS n
              FROM intent_communities
              GROUP BY community_id
              HAVING n = 1
            )
            """
        ).fetchone()["c"]
        return GraphMetrics(
            isolated_ratio=float(isolated_nodes / max(1, total_nodes)),
            uncertain_ratio=float(uncertain_nodes / max(1, isolated_nodes)),
            forced_assignment_ratio=float(forced / max(1, all_assigned)),
            singleton_communities=int(singletons),
            communities=int(communities),
        )
