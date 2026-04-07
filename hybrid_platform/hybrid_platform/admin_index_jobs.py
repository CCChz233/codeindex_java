"""异步 Java 全量索引任务（HTTP 管理面，不经 MCP）。"""

from __future__ import annotations

import re
import threading
import time
import traceback
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable

from .index_build_runner import run_java_full_index_pipeline

_PROGRESS_FIELD_RE = re.compile(r"([a-zA-Z_]+)=([^ ]+)")


def _parse_progress_fields(message: str) -> dict[str, str]:
    return {k: v for k, v in _PROGRESS_FIELD_RE.findall(message)}


def _parse_fraction(value: str) -> tuple[int, int] | None:
    left, sep, right = value.partition("/")
    if not sep or not left.isdigit() or not right.isdigit():
        return None
    return int(left), int(right)


def _clamp_pct(x: float) -> float:
    return max(0.0, min(100.0, x))


@dataclass
class IndexJobRecord:
    job_id: str
    status: str  # queued | running | succeeded | failed
    created_at_ms: int
    updated_at_ms: int
    request: dict[str, Any]
    current_stage: str = "queued"
    percent: float = 0.0
    stage_stats: dict[str, Any] = field(default_factory=dict)
    result: dict[str, Any] | None = None
    error: dict[str, Any] | None = None
    last_messages: list[str] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def to_public_dict(self, *, verbose: bool = False) -> dict[str, Any]:
        with self._lock:
            base: dict[str, Any] = {
                "job_id": self.job_id,
                "status": self.status,
                "current_stage": self.current_stage,
                "percent": round(self.percent, 2),
                "created_at_ms": self.created_at_ms,
                "updated_at_ms": self.updated_at_ms,
                "stage_stats": dict(self.stage_stats),
            }
            if self.result is not None:
                base["result"] = self.result
            if self.error is not None:
                base["error"] = self.error
            if verbose:
                base["request"] = self.request
                base["last_messages"] = list(self.last_messages)
            return base

    def append_message(self, msg: str, *, max_keep: int = 30) -> None:
        with self._lock:
            self.last_messages.append(msg)
            if len(self.last_messages) > max_keep:
                self.last_messages = self.last_messages[-max_keep:]
            self.updated_at_ms = int(time.time() * 1000)

    def set_running(self) -> None:
        with self._lock:
            self.status = "running"
            self.current_stage = "starting"
            self.updated_at_ms = int(time.time() * 1000)

    def apply_progress_message(self, message: str) -> None:
        """根据流水线进度字符串更新 percent / current_stage。"""
        self.append_message(message)
        fields = _parse_progress_fields(message)
        with self._lock:
            self.updated_at_ms = int(time.time() * 1000)
            if "phase=pipeline.stage" in message or message.startswith("phase=pipeline.stage"):
                stage = fields.get("stage", "")
                status = fields.get("status", "")
                self.current_stage = f"{stage}:{status}" if stage else self.current_stage
                if stage == "scip_java" and status == "start":
                    self.percent = _clamp_pct(2.0)
                elif stage == "scip_java" and status == "done":
                    self.percent = _clamp_pct(15.0)
                    self.stage_stats.setdefault("scip_java", {})["status"] = "done"
                elif stage == "ingest" and status == "start":
                    self.percent = _clamp_pct(max(self.percent, 15.0))
                elif stage == "ingest" and status == "done":
                    self.percent = _clamp_pct(30.0)
                    self.stage_stats.setdefault("ingest", {})["status"] = "done"
                elif stage == "chunk" and status == "start":
                    self.percent = _clamp_pct(max(self.percent, 30.0))
                elif stage == "chunk" and status == "done":
                    self.percent = _clamp_pct(70.0)
                    self.stage_stats.setdefault("chunk", {})["status"] = "done"
                elif stage == "embed" and status == "start":
                    self.percent = _clamp_pct(max(self.percent, 70.0))
                elif stage == "embed" and status == "done":
                    self.percent = _clamp_pct(100.0)
                    self.stage_stats.setdefault("embed", {})["status"] = "done"
                return

            if "phase=build_chunks.progress" in message:
                docs = _parse_fraction(fields.get("docs", ""))
                if docs is not None:
                    cur, total = docs
                    if total > 0:
                        self.current_stage = "chunk"
                        self.percent = _clamp_pct(30.0 + 40.0 * (cur / total))
                return

            if "phase=build_chunks.start" in message:
                self.current_stage = "chunk"
                self.percent = _clamp_pct(max(self.percent, 30.0))
                td = fields.get("docs", "")
                if td.isdigit():
                    self.stage_stats.setdefault("chunk", {})["total_docs"] = int(td)
                return

            if "phase=build_chunks.done" in message:
                self.current_stage = "chunk"
                self.percent = _clamp_pct(70.0)
                return

            if "phase=embed.progress" in message:
                batches = _parse_fraction(fields.get("batches", ""))
                if batches is not None:
                    cur, total = batches
                    if total > 0:
                        self.current_stage = "embed"
                        self.percent = _clamp_pct(70.0 + 30.0 * (cur / total))
                return

            if "phase=embed.start" in message:
                self.current_stage = "embed"
                self.percent = _clamp_pct(max(self.percent, 70.0))
                return

            if "phase=embed.done" in message:
                self.current_stage = "embed"
                self.percent = _clamp_pct(100.0)
                return

            if "phase=embed.batch_failed" in message:
                self.stage_stats.setdefault("embed", {}).setdefault("batch_warnings", []).append(
                    message[:500]
                )


_jobs: dict[str, IndexJobRecord] = {}
_jobs_lock = threading.Lock()
_MAX_JOBS = 200


def _prune_jobs() -> None:
    if len(_jobs) <= _MAX_JOBS:
        return
    items = sorted(_jobs.items(), key=lambda kv: kv[1].created_at_ms)
    for jid, _ in items[: len(_jobs) - _MAX_JOBS + 50]:
        _jobs.pop(jid, None)


def get_job(job_id: str) -> IndexJobRecord | None:
    with _jobs_lock:
        return _jobs.get(job_id)


def list_jobs(*, limit: int = 50) -> list[dict[str, Any]]:
    with _jobs_lock:
        items = sorted(_jobs.values(), key=lambda j: j.created_at_ms, reverse=True)
    return [j.to_public_dict(verbose=False) for j in items[:limit]]


def submit_java_full_index(
    body: dict[str, Any],
    *,
    serve_db_path: str | None,
    progress_hook: Callable[[IndexJobRecord, str], None] | None = None,
) -> str:
    repo_root = str(body.get("repo_root") or "").strip()
    repo = str(body.get("repo") or "").strip()
    commit = str(body.get("commit") or "").strip()
    db_path = str(body.get("db_path") or "").strip()
    config_path = body.get("config_path")
    config_path_s = str(config_path).strip() if config_path else None
    config_inline = body.get("config")
    if config_inline is not None and not isinstance(config_inline, dict):
        raise ValueError("config 必须是 JSON 对象")
    if not config_path_s and config_inline is None:
        raise ValueError("必须提供 config_path 或 config")
    if not repo_root or not repo or not commit or not db_path:
        raise ValueError("缺少必填字段: repo_root, repo, commit, db_path")

    job_id = str(uuid.uuid4())
    now = int(time.time() * 1000)
    record = IndexJobRecord(
        job_id=job_id,
        status="queued",
        created_at_ms=now,
        updated_at_ms=now,
        request={
            "repo_root": repo_root,
            "repo": repo,
            "commit": commit,
            "db_path": db_path,
            "config_path": config_path_s,
            "has_inline_config": config_inline is not None,
        },
    )

    def _worker() -> None:
        record.set_running()

        def _cb(msg: str) -> None:
            record.apply_progress_message(msg)
            if progress_hook:
                progress_hook(record, msg)

        try:
            result = run_java_full_index_pipeline(
                repo_root=repo_root,
                repo=repo,
                commit=commit,
                db_path=db_path,
                config_path=config_path_s if config_inline is None else None,
                config_inline=config_inline,
                serve_db_path=serve_db_path,
                progress_callback=_cb,
            )
            with record._lock:
                record.status = "succeeded"
                record.result = result
                record.percent = 100.0
                record.current_stage = "done"
                record.stage_stats = {
                    "scip_java": result.get("scip_java"),
                    "ingest": result.get("ingest"),
                    "chunk": result.get("chunk"),
                    "embed": result.get("embed"),
                }
                record.updated_at_ms = int(time.time() * 1000)
        except Exception as exc:
            tb = traceback.format_exc()
            with record._lock:
                record.status = "failed"
                record.current_stage = "failed"
                record.error = {
                    "type": type(exc).__name__,
                    "message": str(exc),
                    "traceback": tb,
                }
                record.updated_at_ms = int(time.time() * 1000)

    with _jobs_lock:
        _jobs[job_id] = record
        _prune_jobs()

    thread = threading.Thread(target=_worker, name=f"index-job-{job_id[:8]}", daemon=True)
    thread.start()
    return job_id
