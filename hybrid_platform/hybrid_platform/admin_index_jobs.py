"""异步 Java 全量索引任务（HTTP 管理面，不经 MCP）。"""

from __future__ import annotations

import hashlib
import json
import re
import threading
import time
import traceback
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .index_build_runner import load_app_config_for_build, run_java_full_index_pipeline

_PROGRESS_FIELD_RE = re.compile(r"([a-zA-Z_]+)=([^ ]+)")
_MAX_JOBS = 200
_BUILD_CONFIG_SECTIONS = ("ingest", "java_index", "chunk", "embed", "embedding", "vector")


def _parse_progress_fields(message: str) -> dict[str, str]:
    return {k: v for k, v in _PROGRESS_FIELD_RE.findall(message)}


def _parse_fraction(value: str) -> tuple[int, int] | None:
    left, sep, right = value.partition("/")
    if not sep or not left.isdigit() or not right.isdigit():
        return None
    return int(left), int(right)


def _clamp_pct(x: float) -> float:
    return max(0.0, min(100.0, x))


def _normalize_path_key(raw_path: str) -> str:
    try:
        return str(Path(raw_path).expanduser().resolve())
    except OSError:
        return str(Path(raw_path).expanduser())


def _build_config_payload(values: dict[str, Any]) -> dict[str, Any]:
    return {section: values.get(section, {}) for section in _BUILD_CONFIG_SECTIONS}


def _config_fingerprint(*, config_path: str | None, config_inline: dict[str, Any] | None) -> str:
    cfg = load_app_config_for_build(config_path=config_path, config_inline=config_inline)
    payload = _build_config_payload(cfg.values)
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha1(encoded).hexdigest()


class IndexJobConflictError(RuntimeError):
    def __init__(
        self,
        conflict_on: str,
        existing_job_id: str | None = None,
        message: str | None = None,
    ) -> None:
        detail = message or f"index job conflict on {conflict_on}"
        super().__init__(detail)
        self.conflict_on = conflict_on
        self.existing_job_id = existing_job_id


class IndexJobQueueFullError(RuntimeError):
    def __init__(self, max_queue_size: int) -> None:
        super().__init__(f"index job queue is full (max_queue_size={max_queue_size})")
        self.max_queue_size = max_queue_size


@dataclass(frozen=True)
class IndexJobSubmitResult:
    job_id: str
    status: str
    deduped: bool


@dataclass
class IndexJobRecord:
    job_id: str
    status: str  # queued | running | succeeded | failed
    created_at_ms: int
    updated_at_ms: int
    request: dict[str, Any]
    db_key: str = ""
    snapshot_key: str = ""
    config_fingerprint: str = ""
    current_stage: str = "queued"
    percent: float = 0.0
    stage_stats: dict[str, Any] = field(default_factory=dict)
    result: dict[str, Any] | None = None
    error: dict[str, Any] | None = None
    last_messages: list[str] = field(default_factory=list)
    queue_position: int | None = None
    _execution: dict[str, Any] = field(default_factory=dict, repr=False)
    _progress_hook: Callable[["IndexJobRecord", str], None] | None = field(default=None, repr=False)
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
            if self.queue_position is not None and self.status == "queued":
                base["queue_position"] = int(self.queue_position)
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

    def set_queued(self, position: int) -> None:
        with self._lock:
            self.status = "queued"
            self.current_stage = "queued"
            self.queue_position = max(1, int(position))
            self.updated_at_ms = int(time.time() * 1000)

    def set_running(self) -> None:
        with self._lock:
            self.status = "running"
            self.current_stage = "starting"
            self.queue_position = None
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


class IndexJobScheduler:
    def __init__(
        self,
        *,
        max_concurrent_jobs: int = 2,
        max_queue_size: int = 16,
    ) -> None:
        self.max_concurrent_jobs = max(1, int(max_concurrent_jobs))
        self.max_queue_size = max(0, int(max_queue_size))
        self._jobs: dict[str, IndexJobRecord] = {}
        self._queued_job_ids: list[str] = []
        self._running_job_ids: set[str] = set()
        self._occupied_db_keys: dict[str, str] = {}
        self._occupied_snapshot_keys: dict[str, str] = {}
        self._active_signatures: dict[tuple[str, str, str], str] = {}
        self._lock = threading.Lock()

    def get_job(self, job_id: str) -> IndexJobRecord | None:
        with self._lock:
            return self._jobs.get(job_id)

    def list_jobs(self, *, limit: int = 50) -> list[dict[str, Any]]:
        with self._lock:
            items = sorted(self._jobs.values(), key=lambda j: j.created_at_ms, reverse=True)
        return [j.to_public_dict(verbose=False) for j in items[:limit]]

    def submit(
        self,
        body: dict[str, Any],
        *,
        serve_db_path: str | None,
        progress_hook: Callable[[IndexJobRecord, str], None] | None = None,
    ) -> IndexJobSubmitResult:
        req = self._normalize_request(body)
        serve_db_key = _normalize_path_key(serve_db_path) if serve_db_path else ""
        should_start = False
        record: IndexJobRecord | None = None

        with self._lock:
            active_sig = (req["db_key"], req["snapshot_key"], req["config_fingerprint"])
            existing_job_id = self._active_signatures.get(active_sig)
            if existing_job_id is not None:
                existing = self._jobs.get(existing_job_id)
                if existing is not None and existing.status in {"queued", "running"}:
                    status = str(existing.to_public_dict().get("status", existing.status))
                    return IndexJobSubmitResult(job_id=existing_job_id, status=status, deduped=True)

            if serve_db_key and req["db_key"] == serve_db_key:
                raise IndexJobConflictError(
                    "serve_db",
                    None,
                    "db_path points to the DB currently being served; build into a different DB and switch later",
                )

            conflict_job_id = self._occupied_db_keys.get(req["db_key"])
            if conflict_job_id is not None:
                raise IndexJobConflictError(
                    "db_path",
                    conflict_job_id,
                    f"db_path is already occupied by active index job {conflict_job_id}",
                )

            conflict_job_id = self._occupied_snapshot_keys.get(req["snapshot_key"])
            if conflict_job_id is not None:
                raise IndexJobConflictError(
                    "snapshot",
                    conflict_job_id,
                    f"snapshot {req['snapshot_key']} is already occupied by active index job {conflict_job_id}",
                )

            if len(self._running_job_ids) >= self.max_concurrent_jobs and len(self._queued_job_ids) >= self.max_queue_size:
                raise IndexJobQueueFullError(self.max_queue_size)

            job_id = str(uuid.uuid4())
            now = int(time.time() * 1000)
            record = IndexJobRecord(
                job_id=job_id,
                status="queued",
                created_at_ms=now,
                updated_at_ms=now,
                request={
                    "repo_root": req["repo_root"],
                    "repo": req["repo"],
                    "commit": req["commit"],
                    "db_path": req["db_path"],
                    "config_path": req["config_path"],
                    "has_inline_config": req["config_inline"] is not None,
                },
                db_key=req["db_key"],
                snapshot_key=req["snapshot_key"],
                config_fingerprint=req["config_fingerprint"],
                _execution={
                    "repo_root": req["repo_root"],
                    "repo": req["repo"],
                    "commit": req["commit"],
                    "db_path": req["db_path"],
                    "config_path": req["config_path"],
                    "config_inline": req["config_inline"],
                    "serve_db_path": serve_db_path,
                },
                _progress_hook=progress_hook,
            )
            self._jobs[job_id] = record
            self._occupied_db_keys[record.db_key] = job_id
            self._occupied_snapshot_keys[record.snapshot_key] = job_id
            self._active_signatures[(record.db_key, record.snapshot_key, record.config_fingerprint)] = job_id

            if len(self._running_job_ids) < self.max_concurrent_jobs:
                self._mark_running_locked(record)
                should_start = True
            else:
                self._queued_job_ids.append(job_id)
                self._refresh_queue_positions_locked()

            self._prune_jobs_locked()

        if should_start and record is not None:
            self._start_worker(record)
        return IndexJobSubmitResult(job_id=job_id, status="running" if should_start else "queued", deduped=False)

    def _normalize_request(self, body: dict[str, Any]) -> dict[str, Any]:
        repo_root = str(body.get("repo_root") or "").strip()
        repo = str(body.get("repo") or "").strip()
        commit = str(body.get("commit") or "").strip().lower()
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

        return {
            "repo_root": repo_root,
            "repo": repo,
            "commit": commit,
            "db_path": db_path,
            "db_key": _normalize_path_key(db_path),
            "snapshot_key": f"{repo}@{commit}",
            "config_path": config_path_s,
            "config_inline": config_inline,
            "config_fingerprint": _config_fingerprint(
                config_path=config_path_s if config_inline is None else None,
                config_inline=config_inline,
            ),
        }

    def _mark_running_locked(self, record: IndexJobRecord) -> None:
        record.set_running()
        self._running_job_ids.add(record.job_id)

    def _refresh_queue_positions_locked(self) -> None:
        new_queue: list[str] = []
        for position, job_id in enumerate(self._queued_job_ids, start=1):
            record = self._jobs.get(job_id)
            if record is None or record.status != "queued":
                continue
            record.set_queued(position)
            new_queue.append(job_id)
        self._queued_job_ids = new_queue

    def _start_worker(self, record: IndexJobRecord) -> None:
        thread = threading.Thread(
            target=self._worker_entry,
            args=(record,),
            name=f"index-job-{record.job_id[:8]}",
            daemon=True,
        )
        thread.start()

    def _worker_entry(self, record: IndexJobRecord) -> None:
        execution = dict(record._execution)

        def _cb(msg: str) -> None:
            record.apply_progress_message(msg)
            if record._progress_hook:
                record._progress_hook(record, msg)

        result: dict[str, Any] | None = None
        error: dict[str, Any] | None = None
        try:
            result = run_java_full_index_pipeline(
                repo_root=str(execution["repo_root"]),
                repo=str(execution["repo"]),
                commit=str(execution["commit"]),
                db_path=str(execution["db_path"]),
                config_path=execution["config_path"] if execution["config_inline"] is None else None,
                config_inline=execution["config_inline"],
                serve_db_path=execution["serve_db_path"],
                progress_callback=_cb,
            )
        except Exception as exc:
            error = {
                "type": type(exc).__name__,
                "message": str(exc),
                "traceback": traceback.format_exc(),
            }
        self._finish_job(record, result=result, error=error)

    def _finish_job(
        self,
        record: IndexJobRecord,
        *,
        result: dict[str, Any] | None,
        error: dict[str, Any] | None,
    ) -> None:
        to_start: list[IndexJobRecord] = []
        with self._lock:
            now = int(time.time() * 1000)
            with record._lock:
                if error is None:
                    record.status = "succeeded"
                    record.result = result
                    record.error = None
                    record.percent = 100.0
                    record.current_stage = "done"
                    record.stage_stats = {
                        "scip_java": (result or {}).get("scip_java"),
                        "ingest": (result or {}).get("ingest"),
                        "code_graph": (result or {}).get("code_graph"),
                        "chunk": (result or {}).get("chunk"),
                        "embed": (result or {}).get("embed"),
                    }
                else:
                    record.status = "failed"
                    record.current_stage = "failed"
                    record.error = error
                record.updated_at_ms = now
                record.queue_position = None

            self._running_job_ids.discard(record.job_id)
            if self._occupied_db_keys.get(record.db_key) == record.job_id:
                self._occupied_db_keys.pop(record.db_key, None)
            if self._occupied_snapshot_keys.get(record.snapshot_key) == record.job_id:
                self._occupied_snapshot_keys.pop(record.snapshot_key, None)
            sig = (record.db_key, record.snapshot_key, record.config_fingerprint)
            if self._active_signatures.get(sig) == record.job_id:
                self._active_signatures.pop(sig, None)

            while self._queued_job_ids and len(self._running_job_ids) < self.max_concurrent_jobs:
                next_job_id = self._queued_job_ids.pop(0)
                next_record = self._jobs.get(next_job_id)
                if next_record is None or next_record.status != "queued":
                    continue
                self._mark_running_locked(next_record)
                to_start.append(next_record)
            self._refresh_queue_positions_locked()
            self._prune_jobs_locked()

        for next_record in to_start:
            self._start_worker(next_record)

    def _prune_jobs_locked(self) -> None:
        if len(self._jobs) <= _MAX_JOBS:
            return
        terminal_items = [
            (job_id, record)
            for job_id, record in self._jobs.items()
            if record.status not in {"queued", "running"}
        ]
        if not terminal_items:
            return
        terminal_items.sort(key=lambda item: item[1].created_at_ms)
        remove_count = max(0, len(self._jobs) - _MAX_JOBS)
        for job_id, _ in terminal_items[:remove_count]:
            self._jobs.pop(job_id, None)


_scheduler_lock = threading.Lock()
_scheduler = IndexJobScheduler()


def configure_index_job_scheduler(*, max_concurrent_jobs: int = 2, max_queue_size: int = 16) -> None:
    global _scheduler
    with _scheduler_lock:
        _scheduler = IndexJobScheduler(
            max_concurrent_jobs=max_concurrent_jobs,
            max_queue_size=max_queue_size,
        )


def get_job(job_id: str) -> IndexJobRecord | None:
    with _scheduler_lock:
        scheduler = _scheduler
    return scheduler.get_job(job_id)


def list_jobs(*, limit: int = 50) -> list[dict[str, Any]]:
    with _scheduler_lock:
        scheduler = _scheduler
    return scheduler.list_jobs(limit=limit)


def submit_java_full_index(
    body: dict[str, Any],
    *,
    serve_db_path: str | None,
    progress_hook: Callable[[IndexJobRecord, str], None] | None = None,
) -> IndexJobSubmitResult:
    with _scheduler_lock:
        scheduler = _scheduler
    return scheduler.submit(body, serve_db_path=serve_db_path, progress_hook=progress_hook)
