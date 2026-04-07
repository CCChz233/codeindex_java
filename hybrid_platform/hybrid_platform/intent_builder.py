from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Dict, List, Tuple

from .embedding import DeterministicEmbedder
from .prompt import FUNCTION_INTENT_SYSTEM, render_function_intent_user_prompt
from .storage import SqliteStore


INTENT_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS function_intents (
  node_id TEXT PRIMARY KEY,
  intent_text TEXT NOT NULL,
  intent_tags_json TEXT NOT NULL,
  quality_score REAL NOT NULL,
  role_in_chain TEXT NOT NULL,
  fan_in INTEGER NOT NULL,
  fan_out INTEGER NOT NULL,
  chain_depth INTEGER NOT NULL,
  semantic_vec_json TEXT NOT NULL,
  topology_vec_json TEXT NOT NULL,
  fused_vec_json TEXT NOT NULL,
  model_version TEXT NOT NULL,
  prompt_version TEXT NOT NULL,
  cache_key TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_function_intents_role ON function_intents(role_in_chain);

CREATE TABLE IF NOT EXISTS llm_usage_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  node_id TEXT NOT NULL,
  provider TEXT NOT NULL,
  model TEXT NOT NULL,
  prompt_tokens INTEGER NOT NULL DEFAULT 0,
  completion_tokens INTEGER NOT NULL DEFAULT 0,
  total_tokens INTEGER NOT NULL DEFAULT 0,
  estimated_cost REAL NOT NULL DEFAULT 0,
  latency_ms REAL NOT NULL DEFAULT 0,
  created_at_epoch_ms INTEGER NOT NULL
);
"""


@dataclass
class IntentBuildStats:
    total_nodes: int = 0
    built_intents: int = 0
    cache_hits: int = 0
    fallback_used: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    estimated_cost: float = 0.0


def _norm(value: float, upper: float = 10.0) -> float:
    return min(1.0, max(0.0, value / upper))


class FunctionIntentBuilder:
    def __init__(
        self,
        store: SqliteStore,
        embed_dim: int = 128,
        neighbor_top_k: int = 5,
        llm_model: str = "",
        llm_api_base: str = "",
        llm_api_key: str = "",
        llm_timeout_s: int = 30,
        llm_temperature: float = 0.0,
        llm_max_tokens: int = 200,
    ) -> None:
        self.store = store
        self.store.conn.executescript(INTENT_SCHEMA_SQL)
        self.store.conn.commit()
        self.embedder = DeterministicEmbedder(dim=embed_dim)
        self.neighbor_top_k = max(1, int(neighbor_top_k))
        self.llm_model = llm_model
        self.llm_api_base = llm_api_base
        self.llm_api_key = llm_api_key
        self.llm_timeout_s = llm_timeout_s
        self.llm_temperature = llm_temperature
        self.llm_max_tokens = llm_max_tokens

    def _cache_key(self, node_id: str, signature: str, model_version: str, prompt_version: str) -> str:
        raw = f"{node_id}|{signature}|{model_version}|{prompt_version}".encode("utf-8")
        return hashlib.sha1(raw).hexdigest()

    def _fetch_context(self, node_id: str) -> Dict[str, object] | None:
        cur = self.store.conn.execute(
            """
            SELECT node_id, symbol_id, path, signature, fan_in, fan_out
            FROM code_nodes WHERE node_id = ?
            """,
            (node_id,),
        )
        row = cur.fetchone()
        if row is None:
            return None
        callers = self.store.conn.execute(
            """
            SELECT
              e.src_node AS node_id,
              e.weight AS weight,
              cn.signature AS signature,
              COALESCE(fi.role_in_chain, '') AS role,
              COALESCE(fi.intent_text, '') AS intent_text
            FROM code_edges e
            LEFT JOIN code_nodes cn ON cn.node_id = e.src_node
            LEFT JOIN function_intents fi ON fi.node_id = e.src_node
            WHERE e.dst_node = ? AND e.edge_type = 'calls'
            ORDER BY weight DESC
            LIMIT ?
            """,
            (node_id, self.neighbor_top_k),
        ).fetchall()
        callees = self.store.conn.execute(
            """
            SELECT
              e.dst_node AS node_id,
              e.weight AS weight,
              cn.signature AS signature,
              COALESCE(fi.role_in_chain, '') AS role,
              COALESCE(fi.intent_text, '') AS intent_text
            FROM code_edges e
            LEFT JOIN code_nodes cn ON cn.node_id = e.dst_node
            LEFT JOIN function_intents fi ON fi.node_id = e.dst_node
            WHERE e.src_node = ? AND e.edge_type = 'calls'
            ORDER BY weight DESC
            LIMIT ?
            """,
            (node_id, self.neighbor_top_k),
        ).fetchall()
        snippet = self.store.fetch_symbol_definition_snippet(row["symbol_id"]) or {}
        role_hint = self._role_in_chain(int(row["fan_in"]), int(row["fan_out"]))
        return {
            "node_id": row["node_id"],
            "symbol_id": row["symbol_id"],
            "path": row["path"],
            "signature": row["signature"],
            "fan_in": int(row["fan_in"]),
            "fan_out": int(row["fan_out"]),
            "role_hint": role_hint,
            "callers": [c["node_id"] for c in callers],
            "callees": [c["node_id"] for c in callees],
            "caller_details": [
                {
                    "node_id": str(c["node_id"]),
                    "signature": str(c["signature"] or ""),
                    "role": str(c["role"] or ""),
                    "intent_text": str(c["intent_text"] or ""),
                }
                for c in callers
            ],
            "callee_details": [
                {
                    "node_id": str(c["node_id"]),
                    "signature": str(c["signature"] or ""),
                    "role": str(c["role"] or ""),
                    "intent_text": str(c["intent_text"] or ""),
                }
                for c in callees
            ],
            "snippet": str(snippet.get("code", "")),
        }

    @staticmethod
    def _role_in_chain(fan_in: int, fan_out: int) -> str:
        if fan_in == 0 and fan_out >= 3:
            return "entrypoint"
        if fan_in >= 3 and fan_out >= 3:
            return "orchestrator"
        if fan_out == 0 and fan_in >= 1:
            return "leaf"
        if fan_in == 0 and fan_out == 0:
            return "isolated"
        return "core"

    def _llm_summarize(self, context: Dict[str, object]) -> Tuple[str | None, Dict[str, float]]:
        model = self.llm_model.strip()
        api_key = self.llm_api_key.strip()
        api_base = self.llm_api_base.strip()
        if not model:
            return None, {}
        try:
            from litellm import completion, completion_cost  # type: ignore
        except Exception:
            return None, {}

        prompt = render_function_intent_user_prompt(
            signature=str(context["signature"]),
            path=str(context["path"]),
            fan_in=int(context["fan_in"]),
            fan_out=int(context["fan_out"]),
            role_hint=str(context.get("role_hint", "")),
            callers=[str(x) for x in context["callers"]],
            callees=[str(x) for x in context["callees"]],
            caller_details=[dict(x) for x in context.get("caller_details", [])],
            callee_details=[dict(x) for x in context.get("callee_details", [])],
            code_snippet=str(context["snippet"]),
            neighbor_top_k=self.neighbor_top_k,
        )
        try:
            kwargs = {
                "model": model,
                "messages": [
                    {"role": "system", "content": FUNCTION_INTENT_SYSTEM},
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
            text = ""
            if getattr(resp, "choices", None):
                text = str(resp.choices[0].message.content or "").strip()
            usage = getattr(resp, "usage", None)
            prompt_tokens = int(getattr(usage, "prompt_tokens", 0) or 0)
            completion_tokens = int(getattr(usage, "completion_tokens", 0) or 0)
            total_tokens = int(getattr(usage, "total_tokens", 0) or 0)
            try:
                cost = float(completion_cost(completion_response=resp))
            except Exception:
                cost = 0.0
            metrics = {
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "total_tokens": total_tokens,
                "estimated_cost": cost,
                "model": model,
                "provider": "litellm",
            }
            return (text or None), metrics
        except Exception:
            return None, {}

    def _fallback_intent(self, context: Dict[str, object]) -> str:
        role = self._role_in_chain(int(context["fan_in"]), int(context["fan_out"]))
        return (
            f"{context['signature']} acts as {role} in call-chain, "
            f"located at {context['path']}, orchestrating {len(context['callees'])} downstream calls."
        )

    def build(self, model_version: str = "llm-v1", prompt_version: str = "p1") -> IntentBuildStats:
        stats = IntentBuildStats()
        cur = self.store.conn.execute("SELECT node_id, signature FROM code_nodes WHERE node_type = 'function'")
        rows = cur.fetchall()
        stats.total_nodes = len(rows)
        for row in rows:
            node_id = row["node_id"]
            signature = row["signature"]
            cache_key = self._cache_key(node_id, signature, model_version, prompt_version)
            existing = self.store.conn.execute(
                """
                SELECT node_id FROM function_intents
                WHERE node_id = ? AND cache_key = ?
                """,
                (node_id, cache_key),
            ).fetchone()
            if existing:
                stats.cache_hits += 1
                continue

            context = self._fetch_context(node_id)
            if context is None:
                continue

            llm_intent, llm_metrics = self._llm_summarize(context)
            if llm_intent is None:
                llm_intent = self._fallback_intent(context)
                stats.fallback_used += 1
            else:
                stats.prompt_tokens += int(llm_metrics.get("prompt_tokens", 0))
                stats.completion_tokens += int(llm_metrics.get("completion_tokens", 0))
                stats.total_tokens += int(llm_metrics.get("total_tokens", 0))
                stats.estimated_cost += float(llm_metrics.get("estimated_cost", 0.0))
                self.store.conn.execute(
                    """
                    INSERT INTO llm_usage_events(
                      node_id, provider, model, prompt_tokens, completion_tokens, total_tokens,
                      estimated_cost, latency_ms, created_at_epoch_ms
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, strftime('%s','now') * 1000)
                    """,
                    (
                        node_id,
                        str(llm_metrics.get("provider", "litellm")),
                        str(llm_metrics.get("model", self.llm_model)),
                        int(llm_metrics.get("prompt_tokens", 0)),
                        int(llm_metrics.get("completion_tokens", 0)),
                        int(llm_metrics.get("total_tokens", 0)),
                        float(llm_metrics.get("estimated_cost", 0.0)),
                        0.0,
                    ),
                )
            role = self._role_in_chain(int(context["fan_in"]), int(context["fan_out"]))
            tags = [role, str(context["path"]).split("/")[0]]
            semantic_vec = self.embedder.embed(f"{context['signature']}\n{llm_intent}\n{context['snippet']}")
            topology_vec = [
                _norm(float(context["fan_in"])),
                _norm(float(context["fan_out"])),
                _norm(float(len(context["callers"]))),
                _norm(float(len(context["callees"]))),
            ]
            fused_vec = semantic_vec + topology_vec
            quality = 0.85 if "acts as" not in llm_intent else 0.65
            self.store.conn.execute(
                """
                INSERT OR REPLACE INTO function_intents(
                  node_id, intent_text, intent_tags_json, quality_score,
                  role_in_chain, fan_in, fan_out, chain_depth,
                  semantic_vec_json, topology_vec_json, fused_vec_json,
                  model_version, prompt_version, cache_key
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    node_id,
                    llm_intent,
                    json.dumps(tags),
                    quality,
                    role,
                    int(context["fan_in"]),
                    int(context["fan_out"]),
                    2,
                    json.dumps(semantic_vec),
                    json.dumps(topology_vec),
                    json.dumps(fused_vec),
                    model_version,
                    prompt_version,
                    cache_key,
                ),
            )
            stats.built_intents += 1
        self.store.conn.commit()
        return stats
