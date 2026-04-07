"""admin_index_jobs 进度解析与 AppConfig.merge_with_defaults。"""

from hybrid_platform.admin_index_jobs import IndexJobRecord
from hybrid_platform.config import AppConfig


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
