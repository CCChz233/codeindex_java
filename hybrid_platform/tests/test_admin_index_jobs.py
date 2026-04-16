"""admin_index_jobs 调度、冲突与 HTTP 提交响应。"""

from __future__ import annotations

import threading
import time
from http import HTTPStatus
from pathlib import Path

import pytest

from hybrid_platform import admin_index_jobs, service_api
from hybrid_platform.admin_index_jobs import (
    IndexJobConflictError,
    IndexJobQueueFullError,
    IndexJobRecord,
    IndexJobScheduler,
    IndexJobSubmitResult,
)
from hybrid_platform.config import AppConfig
from hybrid_platform.service_api import QueryHandler


def _wait_until(predicate, *, timeout_s: float = 2.0) -> None:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("timed out waiting for predicate")


def _job_status(job_id: str) -> str:
    record = admin_index_jobs.get_job(job_id)
    return record.status if record is not None else ""


def _body(
    tmp_path: Path,
    *,
    repo: str = "demo/repo",
    commit: str = "abc123",
    db_name: str = "index.db",
    config: dict | None = None,
) -> dict[str, object]:
    return {
        "repo_root": str(tmp_path),
        "repo": repo,
        "commit": commit,
        "db_path": str(tmp_path / db_name),
        "config": config
        or {
            "embedding": {"provider": "deterministic", "version": "v1"},
            "vector": {"backend": "sqlite", "write_mode": "sqlite_only", "lancedb": {}},
        },
    }


def _make_blocking_pipeline(
    release_events: dict[str, threading.Event],
    started_events: dict[str, threading.Event],
    calls: list[dict[str, object]],
):
    def _fake_run(
        *,
        repo_root: str,
        repo: str,
        commit: str,
        db_path: str,
        config_path: str | None = None,
        config_inline: dict | None = None,
        serve_db_path: str | None = None,
        progress_callback=None,
    ) -> dict[str, object]:
        calls.append(
            {
                "repo_root": repo_root,
                "repo": repo,
                "commit": commit,
                "db_path": db_path,
                "config_path": config_path,
                "config_inline": config_inline,
                "serve_db_path": serve_db_path,
            }
        )
        started_events.setdefault(db_path, threading.Event()).set()
        if progress_callback is not None:
            progress_callback("phase=pipeline.stage stage=scip_java status=start")
        gate = release_events.setdefault(db_path, threading.Event())
        assert gate.wait(timeout=2.0), f"timed out waiting to release {db_path}"
        if progress_callback is not None:
            progress_callback("phase=pipeline.stage stage=scip_java status=done")
            progress_callback("phase=pipeline.stage stage=ingest status=done")
            progress_callback("phase=pipeline.stage stage=chunk status=done")
            progress_callback("phase=pipeline.stage stage=embed status=done")
        return {
            "ok": True,
            "scip_java": {"output_path": db_path},
            "ingest": {"documents": 1},
            "code_graph": {"nodes": 0, "edges": 0},
            "chunk": {"chunks": 1},
            "embed": {"embedded_chunks": 1},
        }

    return _fake_run


@pytest.fixture(autouse=True)
def _reset_scheduler() -> None:
    admin_index_jobs.configure_index_job_scheduler(max_concurrent_jobs=2, max_queue_size=16)
    QueryHandler.serve_db_path = None
    yield
    QueryHandler.serve_db_path = None
    admin_index_jobs.configure_index_job_scheduler(max_concurrent_jobs=2, max_queue_size=16)


def test_index_job_progress_pipeline_stages() -> None:
    rec = IndexJobRecord(
        job_id="j1",
        status="running",
        created_at_ms=0,
        updated_at_ms=0,
        request={},
    )
    rec.apply_progress_message("phase=pipeline.stage stage=scip_java status=start")
    assert rec.percent >= 0
    rec.apply_progress_message("phase=pipeline.stage stage=scip_java status=done")
    assert rec.percent == 15.0
    rec.apply_progress_message("phase=pipeline.stage stage=ingest status=done")
    assert rec.percent == 30.0
    rec.apply_progress_message("phase=build_chunks.start docs=10 strategy=ast function_level_only=true")
    assert rec.stage_stats.get("chunk", {}).get("total_docs") == 10
    rec.apply_progress_message("phase=build_chunks.progress docs=5/10 chunks=3 path=Foo.java")
    assert rec.percent == 50.0
    rec.apply_progress_message("phase=build_chunks.done docs=10")
    assert rec.percent == 70.0
    rec.apply_progress_message("phase=embed.start batches=4 provider=deterministic")
    rec.apply_progress_message("phase=embed.progress batches=2/4 embedded_chunks=8")
    assert 70.0 < rec.percent < 100.0
    rec.apply_progress_message("phase=embed.done batches=4 embedded_chunks=10")
    assert rec.percent == 100.0


def test_app_config_merge_with_defaults() -> None:
    cfg = AppConfig.merge_with_defaults({"embedding": {"version": "v2"}})
    assert cfg.get("embedding", "version") == "v2"
    assert int(cfg.get("embedding", "batch_size", 0)) > 0
    assert int(cfg.get("admin_index", "max_concurrent_jobs", 0)) == 2
    assert int(cfg.get("admin_index", "max_queue_size", 0)) == 16


def test_scheduler_runs_queues_and_starts_next_job(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    release_events: dict[str, threading.Event] = {}
    started_events: dict[str, threading.Event] = {}
    calls: list[dict[str, object]] = []
    monkeypatch.setattr(
        admin_index_jobs,
        "run_java_full_index_pipeline",
        _make_blocking_pipeline(release_events, started_events, calls),
    )
    admin_index_jobs.configure_index_job_scheduler(max_concurrent_jobs=1, max_queue_size=1)

    body1 = _body(tmp_path, repo="demo/r1", commit="aaa111", db_name="a.db")
    body2 = _body(tmp_path, repo="demo/r2", commit="bbb222", db_name="b.db")
    body3 = _body(tmp_path, repo="demo/r3", commit="ccc333", db_name="c.db")

    submit1 = admin_index_jobs.submit_java_full_index(body1, serve_db_path=None)
    assert submit1.status == "running"
    _wait_until(lambda: bool(started_events.get(str(tmp_path / "a.db"))) and started_events[str(tmp_path / "a.db")].is_set())

    submit2 = admin_index_jobs.submit_java_full_index(body2, serve_db_path=None)
    assert submit2.status == "queued"
    rec2 = admin_index_jobs.get_job(submit2.job_id)
    assert rec2 is not None
    assert rec2.to_public_dict()["queue_position"] == 1
    assert not started_events.get(str(tmp_path / "b.db"), threading.Event()).is_set()

    with pytest.raises(IndexJobQueueFullError):
        admin_index_jobs.submit_java_full_index(body3, serve_db_path=None)

    release_events[str(tmp_path / "a.db")].set()
    _wait_until(lambda: _job_status(submit1.job_id) == "succeeded")
    _wait_until(lambda: bool(started_events.get(str(tmp_path / "b.db"))) and started_events[str(tmp_path / "b.db")].is_set())
    _wait_until(lambda: _job_status(submit2.job_id) == "running")

    release_events[str(tmp_path / "b.db")].set()
    _wait_until(lambda: _job_status(submit2.job_id) == "succeeded")
    assert len(calls) == 2


def test_scheduler_dedupes_identical_active_request(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    release_events: dict[str, threading.Event] = {}
    started_events: dict[str, threading.Event] = {}
    calls: list[dict[str, object]] = []
    monkeypatch.setattr(
        admin_index_jobs,
        "run_java_full_index_pipeline",
        _make_blocking_pipeline(release_events, started_events, calls),
    )
    admin_index_jobs.configure_index_job_scheduler(max_concurrent_jobs=1, max_queue_size=1)

    body = _body(tmp_path, repo="demo/repo", commit="abc123", db_name="same.db")
    first = admin_index_jobs.submit_java_full_index(body, serve_db_path=None)
    _wait_until(lambda: bool(started_events.get(str(tmp_path / "same.db"))) and started_events[str(tmp_path / "same.db")].is_set())

    second = admin_index_jobs.submit_java_full_index(body, serve_db_path=None)
    assert second.deduped is True
    assert second.job_id == first.job_id
    assert second.status == "running"

    release_events[str(tmp_path / "same.db")].set()
    _wait_until(lambda: _job_status(first.job_id) == "succeeded")
    assert len(calls) == 1


def test_scheduler_rejects_conflicting_targets(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    release_events: dict[str, threading.Event] = {}
    started_events: dict[str, threading.Event] = {}
    calls: list[dict[str, object]] = []
    monkeypatch.setattr(
        admin_index_jobs,
        "run_java_full_index_pipeline",
        _make_blocking_pipeline(release_events, started_events, calls),
    )
    admin_index_jobs.configure_index_job_scheduler(max_concurrent_jobs=1, max_queue_size=2)

    base = _body(tmp_path, repo="demo/repo", commit="abc123", db_name="conflict.db")
    submit = admin_index_jobs.submit_java_full_index(base, serve_db_path=None)
    _wait_until(
        lambda: bool(started_events.get(str(tmp_path / "conflict.db")))
        and started_events[str(tmp_path / "conflict.db")].is_set()
    )

    with pytest.raises(IndexJobConflictError) as same_db_diff_snapshot:
        admin_index_jobs.submit_java_full_index(
            _body(tmp_path, repo="demo/other", commit="def456", db_name="conflict.db"),
            serve_db_path=None,
        )
    assert same_db_diff_snapshot.value.conflict_on == "db_path"
    assert same_db_diff_snapshot.value.existing_job_id == submit.job_id

    with pytest.raises(IndexJobConflictError) as same_snapshot_diff_db:
        admin_index_jobs.submit_java_full_index(
            _body(tmp_path, repo="demo/repo", commit="abc123", db_name="different.db"),
            serve_db_path=None,
        )
    assert same_snapshot_diff_db.value.conflict_on == "snapshot"
    assert same_snapshot_diff_db.value.existing_job_id == submit.job_id

    with pytest.raises(IndexJobConflictError) as same_target_diff_config:
        admin_index_jobs.submit_java_full_index(
            _body(
                tmp_path,
                repo="demo/repo",
                commit="abc123",
                db_name="conflict.db",
                config={
                    "embedding": {"provider": "deterministic", "version": "v2"},
                    "vector": {"backend": "sqlite", "write_mode": "sqlite_only", "lancedb": {}},
                },
            ),
            serve_db_path=None,
        )
    assert same_target_diff_config.value.conflict_on == "db_path"
    assert same_target_diff_config.value.existing_job_id == submit.job_id

    release_events[str(tmp_path / "conflict.db")].set()
    _wait_until(lambda: _job_status(submit.job_id) == "succeeded")
    assert len(calls) == 1


def test_scheduler_prune_keeps_active_jobs() -> None:
    scheduler = IndexJobScheduler(max_concurrent_jobs=1, max_queue_size=1)
    for i in range(205):
        status = "succeeded"
        if i == 203:
            status = "running"
        elif i == 204:
            status = "queued"
        record = IndexJobRecord(
            job_id=f"j{i}",
            status=status,
            created_at_ms=i,
            updated_at_ms=i,
            request={},
        )
        scheduler._jobs[record.job_id] = record
        if status == "running":
            scheduler._running_job_ids.add(record.job_id)
        elif status == "queued":
            scheduler._queued_job_ids.append(record.job_id)
    with scheduler._lock:
        scheduler._prune_jobs_locked()
    assert "j203" in scheduler._jobs
    assert "j204" in scheduler._jobs
    assert len(scheduler._jobs) <= 200


def test_submit_admin_index_job_returns_unified_serve_db_conflict(tmp_path: Path) -> None:
    QueryHandler.serve_db_path = str(tmp_path / "live.db")
    payload, status = QueryHandler._submit_admin_index_job(
        _body(tmp_path, repo="demo/repo", commit="abc123", db_name="live.db")
    )
    assert status == HTTPStatus.CONFLICT
    assert payload["error"] == "job_conflict"
    assert payload["conflict_on"] == "serve_db"
    assert payload["existing_job_id"] is None


def test_submit_admin_index_job_success_payload(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    def _submit_ok(body: dict[str, object], *, serve_db_path: str | None) -> IndexJobSubmitResult:
        return IndexJobSubmitResult(job_id="job-123", status="queued", deduped=True)

    monkeypatch.setattr(service_api, "submit_java_full_index", _submit_ok)
    payload, status = QueryHandler._submit_admin_index_job(_body(tmp_path))
    assert status == HTTPStatus.OK
    assert payload == {
        "job_id": "job-123",
        "status": "queued",
        "deduped": True,
        "poll_url_hint": "/admin/index-jobs/job-123",
    }


def test_submit_admin_index_job_maps_queue_full(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    def _raise_queue_full(body: dict[str, object], *, serve_db_path: str | None) -> IndexJobSubmitResult:
        raise IndexJobQueueFullError(7)

    monkeypatch.setattr(service_api, "submit_java_full_index", _raise_queue_full)
    payload, status = QueryHandler._submit_admin_index_job(_body(tmp_path))
    assert status == HTTPStatus.TOO_MANY_REQUESTS
    assert payload["error"] == "queue_full"
    assert payload["max_queue_size"] == 7
