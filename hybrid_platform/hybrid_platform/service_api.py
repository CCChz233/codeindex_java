from __future__ import annotations

import json
import os
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict

from .admin_index_jobs import get_job, list_jobs, submit_java_full_index
from .dsl import Query, callees_of as dsl_callees_of, callers_of as dsl_callers_of, def_of as dsl_def_of, refs_of as dsl_refs_of
from .entity_query import entity_types, find_entity
from .graph_service import GraphService
from .index_contract import IndexContractError, UnsupportedCapabilityError
from .retrieval import HybridRetrievalService
from .runtime_factory import make_embedding_pipeline, make_vector_stores
from .storage import SqliteStore
from .vector_store import SqliteVectorStore
from .vector_store_lancedb import LanceDbVectorStore


class QueryHandler(BaseHTTPRequestHandler):
    service: HybridRetrievalService | None = None
    graph_service: GraphService | None = None
    store: SqliteStore | None = None
    embedding_runtime: Dict[str, object] | None = None
    vector_runtime: Dict[str, object] | None = None
    serve_db_path: str | None = None

    @staticmethod
    def _admin_token_ok(handler: "QueryHandler") -> bool:
        token = os.environ.get("HYBRID_ADMIN_TOKEN", "")
        header = handler.headers.get("X-Admin-Token", "")
        return bool(token) and header == token

    def _json_response(self, payload: Dict[str, Any], status: int = HTTPStatus.OK) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        raw_path = self.path.split("?", 1)[0].rstrip("/") or "/"
        if raw_path == "/health":
            ok_db = False
            if QueryHandler.store is not None:
                try:
                    QueryHandler.store.conn.execute("SELECT 1")
                    ok_db = True
                except Exception:
                    ok_db = False
            self._json_response({"ok": ok_db, "service_ready": QueryHandler.service is not None})
            return
        if raw_path == "/admin/index-jobs" or raw_path.startswith("/admin/index-jobs/"):
            if not QueryHandler._admin_token_ok(self):
                self._json_response({"error": "forbidden"}, status=HTTPStatus.FORBIDDEN)
                return
            if raw_path == "/admin/index-jobs":
                jobs = list_jobs(limit=50)
                self._json_response({"jobs": jobs})
                return
            suffix = raw_path[len("/admin/index-jobs/") :].strip()
            if not suffix:
                self._json_response({"error": "not_found"}, status=HTTPStatus.NOT_FOUND)
                return
            rec = get_job(suffix)
            if rec is None:
                self._json_response({"error": "not_found"}, status=HTTPStatus.NOT_FOUND)
                return
            verbose_q = "verbose=1" in (self.path.split("?", 1)[1] if "?" in self.path else "")
            self._json_response(rec.to_public_dict(verbose=bool(verbose_q)))
            return
        if QueryHandler.service is None:
            self._json_response({"error": "service_not_ready"}, status=HTTPStatus.SERVICE_UNAVAILABLE)
            return
        if self.path.split("?", 1)[0] == "/stats/embedding":
            self._json_response(
                {
                    "runtime": QueryHandler.service.embedding_pipeline.runtime_stats_snapshot(),
                }
            )
            return
        self._json_response({"error": "not_found"}, status=HTTPStatus.NOT_FOUND)

    @staticmethod
    def _serialize_results(results: list[Any]) -> Dict[str, Any]:
        return {
            "results": [
                {
                    "id": r.result_id,
                    "type": r.result_type,
                    "score": r.score,
                    "explain": r.explain,
                    "payload": r.payload,
                }
                for r in results
            ]
        }

    def do_POST(self) -> None:  # noqa: N802
        post_path = self.path.split("?", 1)[0]
        try:
            length = int(self.headers.get("Content-Length", "0"))
            raw_body = self.rfile.read(length).decode("utf-8")
            req = json.loads(raw_body) if raw_body.strip() else {}
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            self._json_response(
                {"error": "bad_request", "message": f"invalid_json: {exc}"},
                status=HTTPStatus.BAD_REQUEST,
            )
            return

        if post_path == "/admin/index-jobs":
            if not QueryHandler._admin_token_ok(self):
                self._json_response({"error": "forbidden"}, status=HTTPStatus.FORBIDDEN)
                return
            try:
                job_id = submit_java_full_index(
                    req,
                    serve_db_path=QueryHandler.serve_db_path,
                )
            except ValueError as exc:
                self._json_response(
                    {"error": "bad_request", "message": str(exc)},
                    status=HTTPStatus.BAD_REQUEST,
                )
                return
            except Exception as exc:
                self._json_response(
                    {"error": "internal_error", "message": str(exc)},
                    status=HTTPStatus.INTERNAL_SERVER_ERROR,
                )
                return
            self._json_response({"job_id": job_id, "poll_url_hint": f"/admin/index-jobs/{job_id}"})
            return

        if QueryHandler.service is None or QueryHandler.graph_service is None:
            self._json_response({"error": "service_not_ready"}, status=HTTPStatus.SERVICE_UNAVAILABLE)
            return
        try:
            if self.path == "/query":
                query = Query(
                    text=req["query"],
                    mode=req.get("mode", "hybrid"),
                    top_k=int(req.get("top_k", 10)),
                    blend_strategy=req.get("blend_strategy", "linear"),
                    filters=req.get("filters", {}),
                )
                include_code = bool(req.get("include_code", False))
                max_code_chars = int(req.get("max_code_chars", 1200))
                results = QueryHandler.service.query(
                    query,
                    include_code=include_code,
                    max_code_chars=max_code_chars,
                )
                self._json_response(self._serialize_results(results))
                return
            if self.path == "/query/structured":
                op = str(req["op"])
                symbol_id = str(req["symbol_id"])
                top_k = int(req.get("top_k", 10))
                include_code = bool(req.get("include_code", False))
                max_code_chars = int(req.get("max_code_chars", 1200))
                query_factory = {
                    "def_of": dsl_def_of,
                    "refs_of": dsl_refs_of,
                    "callers_of": dsl_callers_of,
                    "callees_of": dsl_callees_of,
                }
                if op not in query_factory:
                    raise ValueError(f"unsupported structured op: {op}")
                results = QueryHandler.service.query(
                    query_factory[op](symbol_id, top_k=top_k),
                    include_code=include_code,
                    max_code_chars=max_code_chars,
                )
                self._json_response(self._serialize_results(results))
                return
            if self.path == "/graph/code/subgraph":
                result = QueryHandler.graph_service.code_subgraph(
                    seed_ids=list(req.get("seed_ids", [])),
                    hops=int(req.get("hops", 1)),
                    edge_type=str(req.get("edge_type", "calls")),
                )
                self._json_response(result)
                return
            if self.path == "/graph/intent/subgraph":
                result = QueryHandler.graph_service.intent_subgraph(
                    community_ids=list(req.get("community_ids", []))
                )
                self._json_response(result)
                return
            if self.path == "/graph/intent/explore":
                result = QueryHandler.graph_service.explore(
                    query=req.get("query"),
                    symbol=req.get("symbol"),
                    module_top_k=int(req.get("module_top_k", 5)),
                    function_top_k=int(req.get("function_top_k", 8)),
                    semantic_top_k=int(req.get("semantic_top_k", 8)),
                    seed_fusion=str(req.get("seed_fusion", "rrf")),
                    module_seed_member_top_k=int(req.get("module_seed_member_top_k", 3)),
                    explore_default_hops_module=int(req.get("explore_default_hops_module", 2)),
                    explore_default_hops_function=int(req.get("explore_default_hops_function", 1)),
                    min_seed_score=float(req.get("min_seed_score", 0.0)),
                    edge_type=str(req.get("edge_type", "calls")),
                    hops=int(req["hops"]) if req.get("hops") is not None else None,
                )
                self._json_response(result)
                return
            if self.path == "/find-entity":
                entity_type = str(req["entity_type"])
                name = str(req["name"])
                match = str(req.get("match", "contains"))
                package_contains = str(req.get("package_contains", ""))
                limit = int(req.get("limit", 50))
                hits = find_entity(
                    QueryHandler.store,
                    type=entity_type,
                    name=name,
                    match=match,
                    package_contains=package_contains,
                    limit=limit,
                )
                self._json_response(
                    {
                        "entity_type": entity_type,
                        "name": name,
                        "match": match,
                        "count": len(hits),
                        "entities": [
                            {
                                "symbol_id": h.symbol_id,
                                "display_name": h.display_name,
                                "kind": h.kind,
                                "package": h.package,
                                "language": h.language,
                                "enclosing_symbol": h.enclosing_symbol,
                            }
                            for h in hits
                        ],
                        "supported_types": list(entity_types()),
                    }
                )
                return
            if self.path == "/admin/purge-chunks":
                token = os.environ.get("HYBRID_ADMIN_TOKEN", "")
                header = self.headers.get("X-Admin-Token", "")
                if not token or header != token:
                    self._json_response({"error": "forbidden"}, status=HTTPStatus.FORBIDDEN)
                    return
                if QueryHandler.store is None or QueryHandler.vector_runtime is None:
                    self._json_response({"error": "service_not_ready"}, status=HTTPStatus.SERVICE_UNAVAILABLE)
                    return
                repo = str(req["repo"])
                commit = str(req["commit"])
                force_lance_drop = bool(req.get("lance_drop_table", False))
                store = QueryHandler.store
                store.set_vector_delete_hook(None)
                cur = store.conn.execute("SELECT COUNT(*) AS c FROM chunks")
                total_chunks = int(cur.fetchone()["c"])
                cur = store.conn.execute(
                    """
                    SELECT COUNT(*) AS c FROM chunks c
                    INNER JOIN documents d ON d.document_id = c.document_id
                    WHERE d.repo = ? AND d.commit_hash = ?
                    """,
                    (repo, commit),
                )
                snap_chunks = int(cur.fetchone()["c"])
                _, write_stores = make_vector_stores(store, QueryHandler.vector_runtime)
                lance_action = "none"
                if snap_chunks > 0:
                    for vs in write_stores:
                        if not isinstance(vs, LanceDbVectorStore):
                            continue
                        if force_lance_drop or snap_chunks == total_chunks:
                            vs.drop_table_if_exists()
                            lance_action = "drop_table"
                        else:
                            vs.delete_by_chunk_id_prefix(f"{repo}:{commit}:")
                            lance_action = "delete_by_prefix"
                deleted = store.delete_chunks_for_repo_commit(repo, commit, invoke_vector_hook=False)
                self._json_response(
                    {
                        "deleted_chunks": deleted,
                        "repo": repo,
                        "commit": commit,
                        "chunks_total_before": total_chunks,
                        "chunks_in_snapshot_before": snap_chunks,
                        "lance_vectors": lance_action,
                    }
                )
                return
            self._json_response({"error": "not_found"}, status=HTTPStatus.NOT_FOUND)
        except UnsupportedCapabilityError as exc:
            self._json_response(
                {
                    "error": "unsupported_capability",
                    "detail": str(exc),
                    "capability": exc.capability,
                    "source_mode": exc.source_mode,
                },
                status=HTTPStatus.BAD_REQUEST,
            )
        except IndexContractError as exc:
            self._json_response(
                {"error": "index_contract_error", "detail": str(exc)},
                status=HTTPStatus.BAD_REQUEST,
            )
        except Exception as exc:
            self._json_response(
                {"error": "bad_request", "detail": str(exc)},
                status=HTTPStatus.BAD_REQUEST,
            )


def run_server(
    db_path: str,
    host: str = "0.0.0.0",
    port: int = 8080,
    embedding_runtime: Dict[str, object] | None = None,
    vector_runtime: Dict[str, object] | None = None,
    chunk_runtime: Dict[str, object] | None = None,
    query_runtime: Dict[str, object] | None = None,
    default_embedding_version: str = "v1",
) -> None:
    store = SqliteStore(db_path)
    QueryHandler.store = store
    try:
        QueryHandler.serve_db_path = str(Path(db_path).resolve())
    except OSError:
        QueryHandler.serve_db_path = db_path
    QueryHandler.embedding_runtime = embedding_runtime or {}
    QueryHandler.vector_runtime = vector_runtime or {}
    embedding_pipeline = make_embedding_pipeline(
        store, embedding_runtime, vector_runtime, chunk_runtime=chunk_runtime
    )
    qcfg = query_runtime or {}
    if not isinstance(qcfg, dict):
        qcfg = {}
    QueryHandler.service = HybridRetrievalService(
        store,
        embedding_pipeline=embedding_pipeline,
        default_embedding_version=default_embedding_version,
        test_code_depref_enabled=bool(qcfg.get("test_code_depref_enabled", True)),
        test_code_score_factor=float(qcfg.get("test_code_score_factor", 0.55)),
    )
    QueryHandler.graph_service = GraphService(
        store,
        embedding_pipeline=embedding_pipeline,
        default_embedding_version=default_embedding_version,
    )
    server = ThreadingHTTPServer((host, port), QueryHandler)
    try:
        server.serve_forever()
    finally:
        store.close()
