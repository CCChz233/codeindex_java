from __future__ import annotations

import json
import time
from importlib import import_module
from collections import defaultdict, deque
from dataclasses import dataclass
from typing import Dict, List, Set, Tuple

from .embedding import cosine
from .prompt import MODULE_INTENT_SYSTEM, render_module_intent_user_prompt
from .storage import SqliteStore


COMMUNITY_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS intent_communities (
  community_id TEXT NOT NULL,
  node_id TEXT NOT NULL,
  cohesion_score REAL NOT NULL,
  assign_score REAL NOT NULL DEFAULT 0,
  assignment_mode TEXT NOT NULL DEFAULT 'auto',
  PRIMARY KEY (community_id, node_id)
);
CREATE INDEX IF NOT EXISTS idx_intent_communities_node ON intent_communities(node_id);

CREATE TABLE IF NOT EXISTS module_intents (
  community_id TEXT PRIMARY KEY,
  module_intent TEXT NOT NULL,
  module_tags_json TEXT NOT NULL,
  backbone_json TEXT NOT NULL,
  cohesion_score REAL NOT NULL,
  size INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS intent_community_runs (
  run_id TEXT PRIMARY KEY,
  created_at_epoch_ms INTEGER NOT NULL,
  resolution REAL NOT NULL,
  algorithm_used TEXT NOT NULL,
  community_count INTEGER NOT NULL,
  singleton_communities INTEGER NOT NULL,
  avg_cohesion REAL NOT NULL,
  stability_score REAL NOT NULL,
  objective_score REAL NOT NULL,
  selected INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_intent_community_runs_selected ON intent_community_runs(selected, created_at_epoch_ms);

CREATE TABLE IF NOT EXISTS intent_community_members_history (
  run_id TEXT NOT NULL,
  community_id TEXT NOT NULL,
  node_id TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_intent_community_members_run ON intent_community_members_history(run_id);
"""


@dataclass
class CommunityStats:
    nodes: int = 0
    candidate_edges: int = 0
    communities: int = 0
    singleton_communities: int = 0
    algorithm_used: str = "fallback"
    fallback_reason: str = ""
    semantic_candidate_mode: str = "bruteforce"
    semantic_candidate_fallback_reason: str = ""
    resolution: float = 1.0
    stability_score: float = 0.0
    objective_score: float = 0.0
    selected_run_id: str = ""


class IntentCommunityBuilder:
    def __init__(
        self,
        store: SqliteStore,
        llm_model: str = "",
        llm_api_base: str = "",
        llm_api_key: str = "",
        llm_timeout_s: int = 30,
        llm_temperature: float = 0.0,
        llm_max_tokens: int = 200,
    ) -> None:
        self.store = store
        self.store.conn.executescript(COMMUNITY_SCHEMA_SQL)
        self.store.conn.commit()
        self.llm_model = llm_model
        self.llm_api_base = llm_api_base
        self.llm_api_key = llm_api_key
        self.llm_timeout_s = llm_timeout_s
        self.llm_temperature = llm_temperature
        self.llm_max_tokens = llm_max_tokens

    def _load_intents(self) -> Dict[str, Dict[str, object]]:
        cur = self.store.conn.execute(
            """
            SELECT fi.node_id, fi.intent_text, fi.role_in_chain, fi.semantic_vec_json, fi.quality_score,
                   cn.path, cn.fan_in, cn.fan_out
            FROM function_intents fi
            JOIN code_nodes cn ON cn.node_id = fi.node_id
            """
        )
        result: Dict[str, Dict[str, object]] = {}
        for r in cur.fetchall():
            result[r["node_id"]] = {
                "intent_text": r["intent_text"],
                "role": r["role_in_chain"],
                "semantic_vec": json.loads(r["semantic_vec_json"]),
                "quality": float(r["quality_score"]),
                "path": r["path"],
                "fan_in": int(r["fan_in"]),
                "fan_out": int(r["fan_out"]),
            }
        return result

    def _topology_neighbors(self) -> Dict[str, Set[str]]:
        cur = self.store.conn.execute(
            """
            SELECT src_node, dst_node FROM code_edges
            WHERE edge_type = 'calls'
            """
        )
        graph: Dict[str, Set[str]] = defaultdict(set)
        for r in cur.fetchall():
            graph[r["src_node"]].add(r["dst_node"])
            graph[r["dst_node"]].add(r["src_node"])
        return graph

    @staticmethod
    def _path_prior(a_path: str, b_path: str) -> float:
        a_parts = a_path.split("/")
        b_parts = b_path.split("/")
        common = 0
        for ap, bp in zip(a_parts, b_parts):
            if ap != bp:
                break
            common += 1
        return min(1.0, common / 4.0)

    def _build_weighted_candidates(
        self,
        intents: Dict[str, Dict[str, object]],
        topo: Dict[str, Set[str]],
        alpha: float,
        beta: float,
        gamma: float,
        semantic_top_k: int,
        edge_min_weight: float,
    ) -> tuple[Dict[Tuple[str, str], float], str, str]:
        node_ids = list(intents.keys())
        sem_neighbors, mode, mode_reason = self._semantic_candidates(
            intents=intents,
            semantic_top_k=semantic_top_k,
        )

        weights: Dict[Tuple[str, str], float] = {}
        for src in node_ids:
            candidates = {dst for dst, _ in sem_neighbors[src]} | topo.get(src, set())
            for dst in candidates:
                if src == dst or dst not in intents:
                    continue
                a, b = sorted((src, dst))
                key = (a, b)
                if key in weights:
                    continue
                sem = cosine(intents[a]["semantic_vec"], intents[b]["semantic_vec"])
                topo_sim = 1.0 if (b in topo.get(a, set())) else 0.0
                path = self._path_prior(str(intents[a]["path"]), str(intents[b]["path"]))
                total = alpha * topo_sim + beta * max(0.0, sem) + gamma * path
                if total > edge_min_weight:
                    weights[key] = total
        return weights, mode, mode_reason

    def _semantic_candidates(
        self,
        intents: Dict[str, Dict[str, object]],
        semantic_top_k: int,
    ) -> tuple[Dict[str, List[Tuple[str, float]]], str, str]:
        node_ids = list(intents.keys())
        sem_neighbors: Dict[str, List[Tuple[str, float]]] = defaultdict(list)
        try:
            hnswlib = import_module("hnswlib")
            vectors = [list(intents[n]["semantic_vec"]) for n in node_ids]
            if not vectors:
                return sem_neighbors, "hnswlib", ""
            dim = len(vectors[0])
            index = hnswlib.Index(space="cosine", dim=dim)
            index.init_index(max_elements=len(vectors), ef_construction=100, M=16)
            index.add_items(vectors, list(range(len(vectors))))
            index.set_ef(max(50, semantic_top_k * 2))
            labels, distances = index.knn_query(vectors, k=min(len(vectors), semantic_top_k + 1))
            for i, node_id in enumerate(node_ids):
                for j, dist in zip(labels[i], distances[i]):
                    if j == i:
                        continue
                    nbr = node_ids[int(j)]
                    sim = float(1.0 - dist)
                    sem_neighbors[node_id].append((nbr, sim))
            for n in node_ids:
                sem_neighbors[n].sort(key=lambda x: x[1], reverse=True)
                sem_neighbors[n] = sem_neighbors[n][:semantic_top_k]
            return sem_neighbors, "hnswlib", ""
        except Exception as exc:
            for i, a in enumerate(node_ids):
                for b in node_ids[i + 1 :]:
                    sim = cosine(intents[a]["semantic_vec"], intents[b]["semantic_vec"])
                    sem_neighbors[a].append((b, sim))
                    sem_neighbors[b].append((a, sim))
            for n in node_ids:
                sem_neighbors[n].sort(key=lambda x: x[1], reverse=True)
                sem_neighbors[n] = sem_neighbors[n][:semantic_top_k]
            return sem_neighbors, "bruteforce", str(exc)

    @staticmethod
    def _components_from_weights(weights: Dict[Tuple[str, str], float], threshold: float = 0.35) -> List[Set[str]]:
        adj: Dict[str, Set[str]] = defaultdict(set)
        nodes = set()
        for (a, b), w in weights.items():
            nodes.add(a)
            nodes.add(b)
            if w < threshold:
                continue
            adj[a].add(b)
            adj[b].add(a)
        for n in nodes:
            adj.setdefault(n, set())
        seen = set()
        components = []
        for n in nodes:
            if n in seen:
                continue
            q = deque([n])
            seen.add(n)
            comp = {n}
            while q:
                cur = q.popleft()
                for nxt in adj[cur]:
                    if nxt not in seen:
                        seen.add(nxt)
                        comp.add(nxt)
                        q.append(nxt)
            components.append(comp)
        return components

    @staticmethod
    def _components_by_leiden(
        node_ids: List[str],
        weights: Dict[Tuple[str, str], float],
        resolution: float = 1.0,
    ) -> tuple[List[Set[str]], str]:
        try:
            igraph = import_module("igraph")
            leidenalg = import_module("leidenalg")
        except Exception as exc:
            raise RuntimeError(f"leiden dependencies unavailable: {exc}") from exc

        index = {n: i for i, n in enumerate(node_ids)}
        g = igraph.Graph(n=len(node_ids), directed=False)
        edge_pairs = []
        edge_weights = []
        for (a, b), w in weights.items():
            if a not in index or b not in index:
                continue
            edge_pairs.append((index[a], index[b]))
            edge_weights.append(float(w))
        if edge_pairs:
            g.add_edges(edge_pairs)
        partition = leidenalg.find_partition(
            g,
            leidenalg.RBConfigurationVertexPartition,
            weights=edge_weights if edge_pairs else None,
            resolution_parameter=resolution,
        )
        components = []
        for comm in partition:
            components.append({node_ids[i] for i in comm})
        return components, "leiden"

    def _store_communities(
        self,
        components: List[Set[str]],
        intents: Dict[str, Dict[str, object]],
        weights: Dict[Tuple[str, str], float],
    ) -> CommunityStats:
        self.store.conn.execute("DELETE FROM intent_communities")
        self.store.conn.execute("DELETE FROM module_intents")
        stats = CommunityStats()
        stats.nodes = len(intents)
        stats.candidate_edges = len(weights)
        for idx, comp in enumerate(components):
            community_id = f"intent:community:{idx}"
            comp_list = sorted(comp)
            if len(comp_list) == 1:
                stats.singleton_communities += 1
            edge_w = []
            for i, a in enumerate(comp_list):
                for b in comp_list[i + 1 :]:
                    key = tuple(sorted((a, b)))
                    if key in weights:
                        edge_w.append(weights[key])
            cohesion = float(sum(edge_w) / max(1, len(edge_w)))
            for node_id in comp_list:
                self.store.conn.execute(
                    """
                    INSERT INTO intent_communities(community_id, node_id, cohesion_score, assign_score, assignment_mode)
                    VALUES (?, ?, ?, ?, 'auto')
                    """,
                    (community_id, node_id, cohesion, cohesion),
                )
            intents_text = [str(intents[n]["intent_text"]) for n in comp_list]
            role_buckets = defaultdict(int)
            for n in comp_list:
                role_buckets[str(intents[n]["role"])] += 1
            top_role = sorted(role_buckets.items(), key=lambda x: x[1], reverse=True)[0][0]
            summary = self._module_intent_summary(
                top_role=top_role,
                size=len(comp_list),
                intents_text=intents_text,
                node_ids=comp_list,
                intents=intents,
            )
            tags = [top_role, "intent-community"]
            self.store.conn.execute(
                """
                INSERT INTO module_intents(community_id, module_intent, module_tags_json, backbone_json, cohesion_score, size)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    community_id,
                    summary,
                    json.dumps(tags),
                    json.dumps({"samples": intents_text[:5]}),
                    cohesion,
                    len(comp_list),
                ),
            )
        stats.communities = len(components)
        self.store.conn.commit()
        return stats

    def _module_intent_summary(
        self,
        top_role: str,
        size: int,
        intents_text: List[str],
        node_ids: List[str],
        intents: Dict[str, Dict[str, object]],
    ) -> str:
        model = self.llm_model.strip()
        api_key = self.llm_api_key.strip()
        api_base = self.llm_api_base.strip()
        fallback = f"Module focuses on {top_role} functions and shared execution paths."
        if not model:
            return fallback
        sample_paths = [str(intents[n].get("path", "")) for n in node_ids[:8]]
        sample_roles = [str(intents[n].get("role", "")) for n in node_ids[:8]]
        prompt = render_module_intent_user_prompt(
            community_size=size,
            dominant_role=top_role,
            sample_roles=sample_roles,
            sample_paths=sample_paths,
            representative_function_intents=intents_text,
        )
        try:
            from litellm import completion  # type: ignore
        except Exception:
            return fallback
        try:
            kwargs = {
                "model": model,
                "messages": [
                    {
                        "role": "system",
                        "content": MODULE_INTENT_SYSTEM,
                    },
                    {"role": "user", "content": prompt},
                ],
                "timeout": self.llm_timeout_s,
                "temperature": self.llm_temperature,
                "max_tokens": self.llm_max_tokens,
            }
            if api_key:
                kwargs["api_key"] = api_key
            if api_base:
                kwargs["api_base"] = api_base
            resp = completion(**kwargs)
            if getattr(resp, "choices", None):
                text = str(resp.choices[0].message.content or "").strip()
                if text:
                    return text
            return fallback
        except Exception:
            return fallback

    @staticmethod
    def _mapping_from_components(components: List[Set[str]]) -> Dict[str, str]:
        mapping: Dict[str, str] = {}
        for idx, comp in enumerate(components):
            cid = f"intent:community:{idx}"
            for n in comp:
                mapping[n] = cid
        return mapping

    def _fetch_previous_selected_mapping(self) -> Dict[str, str]:
        run = self.store.conn.execute(
            """
            SELECT run_id
            FROM intent_community_runs
            WHERE selected = 1
            ORDER BY created_at_epoch_ms DESC
            LIMIT 1
            """
        ).fetchone()
        if run is None:
            return {}
        cur = self.store.conn.execute(
            """
            SELECT node_id, community_id
            FROM intent_community_members_history
            WHERE run_id = ?
            """,
            (run["run_id"],),
        )
        return {r["node_id"]: r["community_id"] for r in cur.fetchall()}

    @staticmethod
    def _pairwise_stability(prev_map: Dict[str, str], cur_map: Dict[str, str]) -> float:
        common = sorted(set(prev_map.keys()) & set(cur_map.keys()))
        if len(common) < 2:
            return 0.0
        agree = 0
        total = 0
        # 使用固定窗口对，避免 O(N^2)
        window = min(50, len(common) - 1)
        for i, node in enumerate(common):
            for j in range(i + 1, min(i + 1 + window, len(common))):
                other = common[j]
                prev_same = prev_map[node] == prev_map[other]
                cur_same = cur_map[node] == cur_map[other]
                if prev_same == cur_same:
                    agree += 1
                total += 1
        return float(agree / max(1, total))

    @staticmethod
    def _objective(avg_cohesion: float, singleton_ratio: float, stability_score: float) -> float:
        return 0.55 * avg_cohesion + 0.30 * stability_score - 0.15 * singleton_ratio

    def _record_run(
        self,
        run_id: str,
        resolution: float,
        algorithm_used: str,
        community_count: int,
        singleton_communities: int,
        avg_cohesion: float,
        stability_score: float,
        objective_score: float,
        components: List[Set[str]],
        selected: int,
    ) -> None:
        self.store.conn.execute(
            """
            INSERT OR REPLACE INTO intent_community_runs(
              run_id, created_at_epoch_ms, resolution, algorithm_used,
              community_count, singleton_communities, avg_cohesion, stability_score,
              objective_score, selected
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                int(time.time() * 1000),
                resolution,
                algorithm_used,
                community_count,
                singleton_communities,
                avg_cohesion,
                stability_score,
                objective_score,
                selected,
            ),
        )
        self.store.conn.execute("DELETE FROM intent_community_members_history WHERE run_id = ?", (run_id,))
        for idx, comp in enumerate(components):
            cid = f"intent:community:{idx}"
            self.store.conn.executemany(
                """
                INSERT INTO intent_community_members_history(run_id, community_id, node_id)
                VALUES (?, ?, ?)
                """,
                [(run_id, cid, n) for n in comp],
            )

    def build(
        self,
        alpha: float = 0.5,
        beta: float = 0.4,
        gamma: float = 0.1,
        semantic_top_k: int = 20,
        resolution: float = 1.0,
        resolutions: List[float] | None = None,
        edge_min_weight: float = 0.05,
        fallback_threshold: float = 0.35,
    ) -> CommunityStats:
        intents = self._load_intents()
        topo = self._topology_neighbors()
        weights, mode, mode_reason = self._build_weighted_candidates(
            intents, topo, alpha, beta, gamma, semantic_top_k, edge_min_weight
        )
        prev_map = self._fetch_previous_selected_mapping()
        candidates = resolutions if resolutions else [resolution]
        results: List[Tuple[CommunityStats, List[Set[str]], str, float]] = []
        node_ids = list(intents.keys())
        for idx_res, res in enumerate(candidates):
            stats = CommunityStats()
            stats.nodes = len(intents)
            stats.candidate_edges = len(weights)
            stats.semantic_candidate_mode = mode
            stats.semantic_candidate_fallback_reason = mode_reason
            stats.resolution = float(res)
            try:
                components, algo = self._components_by_leiden(node_ids=node_ids, weights=weights, resolution=float(res))
                stats.algorithm_used = algo
            except Exception as exc:
                components = self._components_from_weights(weights, threshold=fallback_threshold)
                stats.algorithm_used = "fallback"
                stats.fallback_reason = str(exc)
            known = set().union(*components) if components else set()
            for node_id in intents.keys():
                if node_id not in known:
                    components.append({node_id})
            preview = self._store_communities(components, intents, weights)
            cur_map = self._mapping_from_components(components)
            stability = self._pairwise_stability(prev_map, cur_map) if prev_map else 0.0
            singleton_ratio = float(preview.singleton_communities / max(1, preview.communities))
            avg_cohesion = 0.0
            if preview.communities > 0:
                rows = self.store.conn.execute(
                    """
                    SELECT AVG(cohesion_score) AS avg_c
                    FROM (
                      SELECT community_id, MAX(cohesion_score) AS cohesion_score
                      FROM intent_communities
                      GROUP BY community_id
                    )
                    """
                ).fetchone()
                avg_cohesion = float(rows["avg_c"] or 0.0)
            objective = self._objective(avg_cohesion, singleton_ratio, stability)
            preview.algorithm_used = stats.algorithm_used
            preview.fallback_reason = stats.fallback_reason
            preview.semantic_candidate_mode = stats.semantic_candidate_mode
            preview.semantic_candidate_fallback_reason = stats.semantic_candidate_fallback_reason
            preview.resolution = float(res)
            preview.stability_score = stability
            preview.objective_score = objective
            run_id = f"run:{int(time.time()*1000)}:{idx_res}:{res}"
            preview.selected_run_id = run_id
            results.append((preview, components, run_id, avg_cohesion))

        best = max(results, key=lambda x: x[0].objective_score)
        for item, comps, run_id, avg_cohesion in results:
            self._record_run(
                run_id=run_id,
                resolution=item.resolution,
                algorithm_used=item.algorithm_used,
                community_count=item.communities,
                singleton_communities=item.singleton_communities,
                avg_cohesion=float(avg_cohesion),
                stability_score=item.stability_score,
                objective_score=item.objective_score,
                components=comps,
                selected=1 if run_id == best[2] else 0,
            )

        selected_stats, selected_components, selected_run_id, _ = best
        self._store_communities(selected_components, intents, weights)
        selected_stats.selected_run_id = selected_run_id
        self.store.conn.commit()
        return selected_stats
