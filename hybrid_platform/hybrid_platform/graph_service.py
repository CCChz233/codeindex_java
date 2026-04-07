from __future__ import annotations

from collections import defaultdict
from typing import Dict, List, Set

from .embedding import EmbeddingPipeline
from .storage import SqliteStore


class GraphService:
    def __init__(
        self,
        store: SqliteStore,
        embedding_pipeline: EmbeddingPipeline | None = None,
        default_embedding_version: str = "v1",
    ) -> None:
        self.store = store
        self.embedding_pipeline = embedding_pipeline or EmbeddingPipeline(store)
        self.default_embedding_version = default_embedding_version

    def _resolve_node_id(self, symbol_or_node: str) -> str:
        if ":" in symbol_or_node and not symbol_or_node.startswith("scip-"):
            cur = self.store.conn.execute(
                "SELECT node_id FROM code_nodes WHERE node_id = ? LIMIT 1",
                (symbol_or_node,),
            )
            row = cur.fetchone()
            if row is not None:
                return str(row["node_id"])
        cur = self.store.conn.execute(
            "SELECT node_id FROM code_nodes WHERE symbol_id = ? LIMIT 1",
            (symbol_or_node,),
        )
        row = cur.fetchone()
        return str(row["node_id"]) if row is not None else symbol_or_node

    def code_subgraph(self, seed_ids: List[str], hops: int = 1, edge_type: str = "calls") -> Dict[str, object]:
        visited: Set[str] = set(seed_ids)
        frontier = set(seed_ids)
        edges = []
        seen_edges: Set[tuple[str, str, str]] = set()
        for _ in range(max(1, hops)):
            if not frontier:
                break
            q_marks = ",".join(["?"] * len(frontier))
            cur = self.store.conn.execute(
                f"""
                SELECT src_node, dst_node, edge_type, weight, confidence
                FROM code_edges
                WHERE edge_type = ? AND (src_node IN ({q_marks}) OR dst_node IN ({q_marks}))
                """,
                (edge_type, *frontier, *frontier),
            )
            next_frontier = set()
            for r in cur.fetchall():
                src = r["src_node"]
                dst = r["dst_node"]
                edge_key = (str(src), str(dst), str(r["edge_type"]))
                if edge_key not in seen_edges:
                    seen_edges.add(edge_key)
                    edges.append(
                        {
                            "src": src,
                            "dst": dst,
                            "type": r["edge_type"],
                            "weight": r["weight"],
                            "confidence": r["confidence"],
                        }
                    )
                if src not in visited:
                    next_frontier.add(src)
                if dst not in visited:
                    next_frontier.add(dst)
                visited.add(src)
                visited.add(dst)
            frontier = next_frontier
        nodes = []
        if visited:
            q_marks = ",".join(["?"] * len(visited))
            cur = self.store.conn.execute(
                f"""
                SELECT node_id, symbol_id, node_type, path, signature, fan_in, fan_out, is_isolated, isolated_type
                FROM code_nodes
                WHERE node_id IN ({q_marks})
                """,
                tuple(visited),
            )
            nodes = [dict(r) for r in cur.fetchall()]
        return {"nodes": nodes, "edges": edges, "explain": {"hops": hops, "edge_type": edge_type}}

    def intent_subgraph(self, community_ids: List[str]) -> Dict[str, object]:
        if not community_ids:
            return {"nodes": [], "edges": []}
        q_marks = ",".join(["?"] * len(community_ids))
        cur = self.store.conn.execute(
            f"""
            SELECT ic.community_id, ic.node_id, ic.cohesion_score, ic.assign_score, ic.assignment_mode,
                   mi.module_intent
            FROM intent_communities ic
            LEFT JOIN module_intents mi ON mi.community_id = ic.community_id
            WHERE ic.community_id IN ({q_marks})
            """,
            tuple(community_ids),
        )
        rows = cur.fetchall()
        nodes = []
        for r in rows:
            nodes.append(
                {
                    "id": r["node_id"],
                    "community_id": r["community_id"],
                    "module_intent": r["module_intent"],
                    "cohesion": r["cohesion_score"],
                    "assign_score": r["assign_score"],
                    "assignment_mode": r["assignment_mode"],
                }
            )
        return {"nodes": nodes, "edges": [], "explain": {"community_count": len(community_ids)}}

    @staticmethod
    def _rrf_seed_fusion(
        rank_sources: Dict[str, List[Dict[str, object]]],
        k: int = 60,
    ) -> Dict[str, Dict[str, object]]:
        merged: Dict[str, Dict[str, object]] = {}
        for source_name, rows in rank_sources.items():
            for rank, row in enumerate(rows, start=1):
                node_id = str(row["node_id"])
                base = merged.setdefault(
                    node_id,
                    {
                        "node_id": node_id,
                        "score": 0.0,
                        "sources": [],
                        "raw_scores": {},
                        "community_ids": set(),
                        "signature": row.get("signature", ""),
                        "path": row.get("path", ""),
                    },
                )
                base["score"] += 1.0 / (k + rank)
                base["sources"].append(source_name)
                base["raw_scores"][source_name] = float(row.get("score", 0.0))
                for cid in row.get("community_ids", []):
                    base["community_ids"].add(cid)
        return merged

    @staticmethod
    def _linear_seed_fusion(rank_sources: Dict[str, List[Dict[str, object]]]) -> Dict[str, Dict[str, object]]:
        weights = {"module": 0.45, "function": 0.30, "semantic": 0.25}
        merged: Dict[str, Dict[str, object]] = {}
        for source_name, rows in rank_sources.items():
            for row in rows:
                node_id = str(row["node_id"])
                base = merged.setdefault(
                    node_id,
                    {
                        "node_id": node_id,
                        "score": 0.0,
                        "sources": [],
                        "raw_scores": {},
                        "community_ids": set(),
                        "signature": row.get("signature", ""),
                        "path": row.get("path", ""),
                    },
                )
                base["score"] += weights.get(source_name, 0.0) * float(row.get("score", 0.0))
                base["sources"].append(source_name)
                base["raw_scores"][source_name] = float(row.get("score", 0.0))
                for cid in row.get("community_ids", []):
                    base["community_ids"].add(cid)
        return merged

    def _module_seed_hits(self, query: str, module_top_k: int, module_seed_member_top_k: int) -> tuple[List[Dict[str, object]], List[Dict[str, object]]]:
        module_hits = self.store.search_module_intents(query, module_top_k)
        seed_rows: List[Dict[str, object]] = []
        for hit in module_hits:
            community_id = str(hit["community_id"])
            members = self.store.fetch_community_seed_nodes(community_id, module_seed_member_top_k)
            member_ids = [str(m["node_id"]) for m in members]
            hit["seed_nodes"] = member_ids
            for member in members:
                seed_rows.append(
                    {
                        "node_id": member["node_id"],
                        "score": float(hit["score"]) + 0.2 * float(member.get("assign_score", 0.0)),
                        "community_ids": [community_id],
                        "signature": member.get("signature", ""),
                        "path": member.get("path", ""),
                    }
                )
        return module_hits, seed_rows

    def _function_seed_hits(self, query: str, function_top_k: int) -> List[Dict[str, object]]:
        return self.store.search_function_intents(query, function_top_k)

    def _semantic_seed_hits(self, query: str, semantic_top_k: int, embedding_version: str) -> List[Dict[str, object]]:
        semantic_hits: List[Dict[str, object]] = []
        by_node: Dict[str, Dict[str, object]] = {}
        for chunk_id, score in self.embedding_pipeline.semantic_search(query, embedding_version, semantic_top_k):
            meta = self.store.fetch_chunk_metadata(chunk_id, include_content=False) or {}
            for symbol_id in self.store.fetch_chunk_primary_symbols(chunk_id):
                node_id = self._resolve_node_id(symbol_id)
                base = by_node.get(node_id)
                if base is None or float(score) > float(base["score"]):
                    by_node[node_id] = {
                        "node_id": node_id,
                        "score": float(score),
                        "chunk_id": chunk_id,
                        "path": meta.get("path", ""),
                        "document_id": meta.get("document_id", ""),
                        "community_ids": [],
                    }
        semantic_hits.extend(by_node.values())
        semantic_hits.sort(key=lambda x: float(x["score"]), reverse=True)
        return semantic_hits[:semantic_top_k]

    def explore(
        self,
        query: str | None = None,
        symbol: str | None = None,
        module_top_k: int = 5,
        function_top_k: int = 8,
        semantic_top_k: int = 8,
        seed_fusion: str = "rrf",
        module_seed_member_top_k: int = 3,
        explore_default_hops_module: int = 2,
        explore_default_hops_function: int = 1,
        min_seed_score: float = 0.0,
        edge_type: str = "calls",
        hops: int | None = None,
        embedding_version: str | None = None,
    ) -> Dict[str, object]:
        if symbol:
            seed = self._resolve_node_id(symbol)
            result = self.code_subgraph([seed], hops=2, edge_type=edge_type)
            result["seed_nodes"] = [{"node_id": seed, "score": 1.0, "sources": ["symbol"], "raw_scores": {}}]
            result["seed_communities"] = []
            result["explain"].update({"query": None, "seed_strategy": "symbol"})
            return result
        if not query:
            return {"nodes": [], "edges": [], "seed_nodes": [], "seed_communities": [], "explain": {"query": None}}
        version = embedding_version or self.default_embedding_version
        module_hits, module_seed_rows = self._module_seed_hits(query, module_top_k, module_seed_member_top_k)
        function_hits = self._function_seed_hits(query, function_top_k)
        semantic_hits = self._semantic_seed_hits(query, semantic_top_k, version)
        rank_sources = {
            "module": module_seed_rows,
            "function": function_hits,
            "semantic": semantic_hits,
        }
        merged = (
            self._rrf_seed_fusion(rank_sources)
            if seed_fusion == "rrf"
            else self._linear_seed_fusion(rank_sources)
        )
        selected = [
            {
                "node_id": node_id,
                "score": float(data["score"]),
                "sources": sorted(set(data["sources"])),
                "raw_scores": data["raw_scores"],
                "community_ids": sorted(data["community_ids"]),
                "signature": data.get("signature", ""),
                "path": data.get("path", ""),
            }
            for node_id, data in merged.items()
            if float(data["score"]) >= min_seed_score
        ]
        selected.sort(key=lambda x: float(x["score"]), reverse=True)
        seed_limit = max(1, module_top_k + function_top_k)
        selected = selected[:seed_limit]
        selected_seed_ids = [str(r["node_id"]) for r in selected]
        used_hops = hops
        if used_hops is None:
            used_hops = explore_default_hops_module if module_hits else explore_default_hops_function
        if not selected_seed_ids:
            return {
                "nodes": [],
                "edges": [],
                "seed_nodes": [],
                "seed_communities": module_hits,
                "explain": {
                    "query": query,
                    "seed_strategy": seed_fusion,
                    "module_hits": module_hits,
                    "function_hits": function_hits,
                    "semantic_hits": semantic_hits,
                    "selected_seeds": [],
                    "hops": used_hops,
                    "edge_type": edge_type,
                    "miss_reason": "no_seed_hits_after_fusion",
                },
            }
        result = self.code_subgraph(selected_seed_ids, hops=used_hops, edge_type=edge_type)
        result["seed_nodes"] = selected
        result["seed_communities"] = module_hits
        result["explain"].update(
            {
                "query": query,
                "seed_strategy": seed_fusion,
                "module_hits": module_hits,
                "function_hits": function_hits,
                "semantic_hits": semantic_hits,
                "selected_seeds": selected,
            }
        )
        return result
