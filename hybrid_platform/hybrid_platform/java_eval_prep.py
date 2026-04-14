"""为 Java 测评 manifest 准备源码工作树、索引清单、批量构建与校验。

典型用途：

1. 从 JSONL manifest 提取唯一的 ``repo + base_sha`` 目标，并推导：
   - worktree 路径
   - SQLite DB 路径
   - MCP 路径
   - 现有 metadata / DB 可复用状态
2. 复用现有 ``repo_commit_to_index.sh`` 批量 clone + 建索引
3. 对已生成索引做最小 smoke 校验，确认不是“只有文件，没有可查询数据”
4. 为测评框架输出 ``sample_id -> repo/base_sha/worktree/db/mcp`` 路由映射
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .entity_query import find_entity
from .index_metadata import IndexMetadataEntry, load_metadata
from .index_slug import default_index_dir, index_db_path, mcp_http_path, repo_commit_slug
from .storage import SqliteStore

HYBRID_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG_PATH = HYBRID_ROOT / "config" / "default_config.json"
DEFAULT_BATCH_SCRIPT = HYBRID_ROOT / "scripts" / "repo_commit_to_index.sh"
REQUIRED_DB_TABLES = ("documents", "symbols", "occurrences", "chunks", "embeddings")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json_dump(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2)


WORKSPACE_ROOT = HYBRID_ROOT.parent / "workspace"


def preferred_data_root() -> Path:
    """大文件优先落在 workspace/；若不存在则回退到仓库内 ``var/java_eval``。"""
    ws = WORKSPACE_ROOT
    if ws.exists():
        return ws
    for base in (Path("/data1/qadong"), Path("/data/qadong")):
        if base.exists():
            return base / "java_eval"
    return HYBRID_ROOT / "var" / "java_eval"


def default_state_root() -> Path:
    if WORKSPACE_ROOT.exists():
        return WORKSPACE_ROOT
    return HYBRID_ROOT / "var" / "java_eval"


def default_worktrees_root() -> Path:
    return preferred_data_root() / "worktrees"


def default_logs_root() -> Path:
    return default_state_root() / "logs"


def default_manifests_root() -> Path:
    return default_state_root() / "manifests"


def default_tmp_root() -> Path:
    ws_tmp = WORKSPACE_ROOT / "cache" / "tmp"
    if ws_tmp.exists():
        return ws_tmp
    for path in (Path("/data1/qadong/tmp"), Path("/data1/tmp"), preferred_data_root() / "tmp"):
        if path.exists():
            return path
    return default_state_root() / "tmp"


def default_targets_path(manifest_path: str | Path) -> Path:
    return default_manifests_root() / f"{Path(manifest_path).stem}.targets.json"


def default_routes_path(manifest_path: str | Path) -> Path:
    return default_manifests_root() / f"{Path(manifest_path).stem}.routes.json"


def default_build_report_path(manifest_path: str | Path) -> Path:
    return default_manifests_root() / f"{Path(manifest_path).stem}.build_report.json"


def default_validation_report_path(manifest_path: str | Path) -> Path:
    return default_manifests_root() / f"{Path(manifest_path).stem}.validation.json"


@dataclass(frozen=True)
class ManifestSample:
    sample_id: str
    repo: str
    repo_url: str
    base_sha: str
    language: str = ""
    difficulty: str = ""
    task_type: str = ""
    repo_type: str = ""


@dataclass
class TargetOverride:
    config_path: str = ""
    build_tool: str = ""
    java_home: str = ""
    recurse_submodules: bool | None = None
    clone_shallow: bool | None = None
    build_args: list[str] = field(default_factory=list)
    build_env: dict[str, str] = field(default_factory=dict)
    docker_packages: list[str] = field(default_factory=list)
    docker_pre_script: str = ""
    pilot: bool = False
    skip: bool = False
    notes: str = ""

    def merged_with(self, other: TargetOverride) -> TargetOverride:
        merged_env = dict(self.build_env)
        merged_env.update(other.build_env or {})
        merged = TargetOverride(
            config_path=other.config_path or self.config_path,
            build_tool=other.build_tool or self.build_tool,
            java_home=other.java_home or self.java_home,
            recurse_submodules=self.recurse_submodules
            if other.recurse_submodules is None
            else other.recurse_submodules,
            clone_shallow=self.clone_shallow if other.clone_shallow is None else other.clone_shallow,
            build_args=list(other.build_args or self.build_args),
            build_env=merged_env,
            docker_packages=list(other.docker_packages or self.docker_packages),
            docker_pre_script=other.docker_pre_script or self.docker_pre_script,
            pilot=bool(self.pilot or other.pilot),
            skip=bool(self.skip or other.skip),
            notes=other.notes or self.notes,
        )
        return merged

    @classmethod
    def from_json_dict(cls, raw: Any) -> TargetOverride:
        if not isinstance(raw, dict):
            return cls()
        build_args = raw.get("build_args", [])
        if not isinstance(build_args, list):
            build_args = []
        build_env = raw.get("build_env", {})
        if not isinstance(build_env, dict):
            build_env = {}
        docker_packages = raw.get("docker_packages", [])
        if not isinstance(docker_packages, list):
            docker_packages = []
        return cls(
            config_path=str(raw.get("config_path", "")).strip(),
            build_tool=str(raw.get("build_tool", "")).strip(),
            java_home=str(raw.get("java_home", "")).strip(),
            recurse_submodules=(
                None if "recurse_submodules" not in raw else bool(raw.get("recurse_submodules"))
            ),
            clone_shallow=None if "clone_shallow" not in raw else bool(raw.get("clone_shallow")),
            build_args=[str(x) for x in build_args if str(x).strip()],
            build_env={str(k): str(v) for k, v in build_env.items() if str(k).strip()},
            docker_packages=[str(x) for x in docker_packages if str(x).strip()],
            docker_pre_script=str(raw.get("docker_pre_script", "")).strip(),
            pilot=bool(raw.get("pilot", False)),
            skip=bool(raw.get("skip", False)),
            notes=str(raw.get("notes", "")).strip(),
        )


@dataclass
class PreparedTarget:
    repo: str
    repo_url: str
    base_sha: str
    slug: str
    sample_ids: list[str]
    sample_count: int
    worktree_path: str
    computed_db_path: str
    effective_db_path: str
    metadata_db_path: str
    lancedb_path: str
    mcp_path: str
    metadata_status: str
    index_status: str
    db_exists: bool
    reusable_index: bool
    config_path: str
    build_tool: str
    java_home: str
    recurse_submodules: bool
    clone_shallow: bool
    build_args: list[str]
    build_env: dict[str, str]
    docker_packages: list[str]
    docker_pre_script: str
    pilot: bool
    skip: bool
    notes: str
    language_values: list[str]
    difficulty_values: list[str]
    task_type_values: list[str]
    repo_type_values: list[str]

    def to_json_dict(self) -> dict[str, Any]:
        return asdict(self)


def _write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_json_dump(data) + "\n", encoding="utf-8")


def _load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def load_manifest_samples(path: str | Path) -> list[ManifestSample]:
    samples: list[ManifestSample] = []
    p = Path(path)
    with p.open(encoding="utf-8") as f:
        for line_no, raw_line in enumerate(f, start=1):
            line = raw_line.strip()
            if not line:
                continue
            data = json.loads(line)
            if not isinstance(data, dict):
                raise ValueError(f"{p}:{line_no}: JSON line must be an object")
            required = ("id", "repo", "repo_url", "base_sha")
            missing = [key for key in required if not str(data.get(key, "")).strip()]
            if missing:
                raise ValueError(f"{p}:{line_no}: missing required keys: {', '.join(missing)}")
            samples.append(
                ManifestSample(
                    sample_id=str(data["id"]).strip(),
                    repo=str(data["repo"]).strip(),
                    repo_url=str(data["repo_url"]).strip(),
                    base_sha=str(data["base_sha"]).strip().lower(),
                    language=str(data.get("language", "")).strip(),
                    difficulty=str(data.get("difficulty", "")).strip(),
                    task_type=str(data.get("task_type", "")).strip(),
                    repo_type=str(data.get("repo_type", "")).strip(),
                )
            )
    return samples


def load_overrides(
    path: str | Path | None,
) -> tuple[TargetOverride, dict[str, TargetOverride]]:
    if not path:
        return TargetOverride(), {}
    p = Path(path)
    if not p.is_file():
        raise FileNotFoundError(f"overrides file not found: {p}")
    data = _load_json(p)
    if not isinstance(data, dict):
        raise ValueError("overrides file must be a JSON object")
    defaults = TargetOverride.from_json_dict(data.get("defaults", {}))
    target_map: dict[str, TargetOverride] = {}
    raw_targets = data.get("targets", {})
    if isinstance(raw_targets, dict):
        for key, value in raw_targets.items():
            target_map[str(key)] = TargetOverride.from_json_dict(value)
    return defaults, target_map


def _target_override_for(
    repo: str,
    base_sha: str,
    slug: str,
    defaults: TargetOverride,
    target_overrides: dict[str, TargetOverride],
) -> TargetOverride:
    override = defaults
    for key in (slug, f"{repo}@{base_sha}", f"{repo}|{base_sha}"):
        specific = target_overrides.get(key)
        if specific is not None:
            override = override.merged_with(specific)
    return override


def _index_status_for(
    entry: IndexMetadataEntry | None,
    computed_db_path: Path,
) -> tuple[str, str, str, bool, bool]:
    metadata_db_path = str(Path(entry.db_path).resolve()) if entry else ""
    metadata_status = str(entry.status) if entry else "missing"
    metadata_db_exists = bool(entry and Path(entry.db_path).is_file())
    computed_exists = computed_db_path.is_file()

    if metadata_db_exists and entry is not None and metadata_status == "ready":
        effective_db_path = str(Path(entry.db_path).resolve())
        return "ready", effective_db_path, metadata_db_path, True, True
    if entry is not None and not metadata_db_exists:
        effective_db_path = str(Path(entry.db_path).resolve()) if entry.db_path else str(computed_db_path.resolve())
        return "stale_metadata", effective_db_path, metadata_db_path, False, False
    if computed_exists:
        effective_db_path = str(computed_db_path.resolve())
        return "db_only", effective_db_path, metadata_db_path, True, False
    return "missing", str(computed_db_path.resolve()), metadata_db_path, False, False


def derive_targets(
    manifest_path: str | Path,
    *,
    worktrees_root: str | Path | None = None,
    index_output_dir: str | Path | None = None,
    metadata_file: str | Path | None = None,
    config_path: str | Path | None = None,
    overrides_path: str | Path | None = None,
) -> tuple[list[PreparedTarget], list[ManifestSample]]:
    samples = load_manifest_samples(manifest_path)
    work_root = Path(worktrees_root) if worktrees_root else default_worktrees_root()
    out_root = Path(index_output_dir) if index_output_dir else default_index_dir()
    meta = load_metadata(Path(metadata_file) if metadata_file else None)
    metadata_by_slug = {entry.slug: entry for entry in meta.entries}
    default_override, target_overrides = load_overrides(overrides_path)
    cfg_default = str(Path(config_path).resolve()) if config_path else str(DEFAULT_CONFIG_PATH.resolve())

    grouped: dict[tuple[str, str], list[ManifestSample]] = defaultdict(list)
    for sample in samples:
        grouped[(sample.repo, sample.base_sha)].append(sample)

    targets: list[PreparedTarget] = []
    for (repo, base_sha), group in sorted(grouped.items(), key=lambda item: (item[0][0], item[0][1])):
        slug = repo_commit_slug(repo, base_sha)
        computed_db = index_db_path(repo, base_sha, out_root)
        entry = metadata_by_slug.get(slug)
        index_status, effective_db, metadata_db_path, db_exists, reusable = _index_status_for(entry, computed_db)
        override = _target_override_for(repo, base_sha, slug, default_override, target_overrides)
        targets.append(
            PreparedTarget(
                repo=repo,
                repo_url=group[0].repo_url,
                base_sha=base_sha,
                slug=slug,
                sample_ids=sorted(sample.sample_id for sample in group),
                sample_count=len(group),
                worktree_path=str((work_root / slug).resolve()),
                computed_db_path=str(computed_db.resolve()),
                effective_db_path=effective_db,
                metadata_db_path=metadata_db_path,
                lancedb_path=f"{effective_db}.lancedb",
                mcp_path=mcp_http_path(repo, base_sha),
                metadata_status=(entry.status if entry else "missing"),
                index_status=index_status,
                db_exists=db_exists,
                reusable_index=reusable,
                config_path=str(Path(override.config_path).resolve()) if override.config_path else cfg_default,
                build_tool=override.build_tool,
                java_home=override.java_home,
                recurse_submodules=bool(override.recurse_submodules),
                clone_shallow=bool(override.clone_shallow),
                build_args=list(override.build_args),
                build_env=dict(override.build_env),
                docker_packages=list(override.docker_packages),
                docker_pre_script=override.docker_pre_script,
                pilot=bool(override.pilot),
                skip=bool(override.skip),
                notes=override.notes,
                language_values=sorted({sample.language for sample in group if sample.language}),
                difficulty_values=sorted({sample.difficulty for sample in group if sample.difficulty}),
                task_type_values=sorted({sample.task_type for sample in group if sample.task_type}),
                repo_type_values=sorted({sample.repo_type for sample in group if sample.repo_type}),
            )
        )
    return targets, samples


def build_routes(samples: list[ManifestSample], targets: list[PreparedTarget]) -> list[dict[str, Any]]:
    target_by_slug = {target.slug: target for target in targets}
    routes: list[dict[str, Any]] = []
    for sample in samples:
        slug = repo_commit_slug(sample.repo, sample.base_sha)
        target = target_by_slug[slug]
        routes.append(
            {
                "sample_id": sample.sample_id,
                "repo": sample.repo,
                "repo_url": sample.repo_url,
                "base_sha": sample.base_sha,
                "language": sample.language,
                "difficulty": sample.difficulty,
                "task_type": sample.task_type,
                "repo_type": sample.repo_type,
                "slug": target.slug,
                "worktree_path": target.worktree_path,
                "db_path": target.effective_db_path,
                "mcp_path": target.mcp_path,
                "config_path": target.config_path,
                "index_status": target.index_status,
                "metadata_status": target.metadata_status,
            }
        )
    return routes


def render_targets_doc(
    manifest_path: str | Path,
    targets: list[PreparedTarget],
    *,
    worktrees_root: str | Path | None,
    index_output_dir: str | Path | None,
    metadata_file: str | Path | None,
    config_path: str | Path | None,
    overrides_path: str | Path | None,
) -> dict[str, Any]:
    status_counts = Counter(target.index_status for target in targets)
    return {
        "meta": {
            "generated_at": _now_iso(),
            "manifest_path": str(Path(manifest_path).resolve()),
            "worktrees_root": str((Path(worktrees_root) if worktrees_root else default_worktrees_root()).resolve()),
            "index_output_dir": str((Path(index_output_dir) if index_output_dir else default_index_dir()).resolve()),
            "metadata_file": str(
                (Path(metadata_file) if metadata_file else HYBRID_ROOT / "var" / "index_metadata.json").resolve()
            ),
            "default_config_path": str((Path(config_path) if config_path else DEFAULT_CONFIG_PATH).resolve()),
            "overrides_path": str(Path(overrides_path).resolve()) if overrides_path else "",
            "hybrid_root": str(HYBRID_ROOT.resolve()),
        },
        "summary": {
            "unique_targets": len(targets),
            "status_counts": dict(status_counts),
            "pilot_targets": [target.slug for target in targets if target.pilot],
            "skipped_targets": [target.slug for target in targets if target.skip],
        },
        "targets": [target.to_json_dict() for target in targets],
    }


def render_routes_doc(
    manifest_path: str | Path,
    routes: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "meta": {
            "generated_at": _now_iso(),
            "manifest_path": str(Path(manifest_path).resolve()),
            "sample_count": len(routes),
        },
        "routes": routes,
    }


def _load_targets_from_manifest_args(args: argparse.Namespace) -> tuple[list[PreparedTarget], list[ManifestSample]]:
    return derive_targets(
        args.manifest,
        worktrees_root=getattr(args, "worktrees_root", None),
        index_output_dir=getattr(args, "index_output_dir", None),
        metadata_file=getattr(args, "metadata_file", None),
        config_path=getattr(args, "config", None),
        overrides_path=getattr(args, "overrides", None),
    )


def _selected_targets(targets: list[PreparedTarget], args: argparse.Namespace) -> list[PreparedTarget]:
    selected = list(targets)

    slugs = set(getattr(args, "slug", []) or [])
    if slugs:
        selected = [target for target in selected if target.slug in slugs]

    repos = set(getattr(args, "repo", []) or [])
    if repos:
        selected = [target for target in selected if target.repo in repos]

    sample_ids = set(getattr(args, "sample_id", []) or [])
    if sample_ids:
        selected = [target for target in selected if sample_ids.intersection(target.sample_ids)]

    if bool(getattr(args, "only_pilot", False)):
        selected = [target for target in selected if target.pilot]

    limit = int(getattr(args, "limit", 0) or 0)
    if limit > 0:
        selected = selected[:limit]

    return selected


def _build_command_for_target(
    target: PreparedTarget,
    *,
    batch_script: Path,
    index_output_dir: Path,
) -> list[str]:
    cmd = [
        str(batch_script),
        "--config",
        target.config_path,
        "--repo-name",
        target.repo,
        "--commit",
        target.base_sha,
        "--dest",
        target.worktree_path,
        "--git-url",
        target.repo_url,
        "--output-dir",
        str(index_output_dir),
    ]
    if target.build_tool:
        cmd.extend(["--build-tool", target.build_tool])
    if target.java_home:
        cmd.extend(["--java-home", target.java_home])
    if target.recurse_submodules:
        cmd.append("--recurse-submodules")
    if target.clone_shallow:
        cmd.append("--clone-shallow")
    if target.build_args:
        cmd.append("--")
        cmd.extend(target.build_args)
    return cmd


def _ensure_parent_dirs(*paths: Path) -> None:
    for path in paths:
        path.mkdir(parents=True, exist_ok=True)


def _discover_maven_bin() -> str:
    candidates: list[Path] = []
    patterns = [
        Path("/root/.m2/wrapper/dists").glob("**/bin/mvn"),
        Path("/data1/qadong").glob("**/apache-maven-*/bin/mvn"),
    ]
    for iterator in patterns:
        for mvn in iterator:
            if mvn.is_file():
                candidates.append(mvn.resolve())
    if not candidates:
        return ""
    candidates = sorted(candidates, key=lambda p: str(p), reverse=True)
    return str(candidates[0].parent)


def _augment_env_for_build(env: dict[str, str], target: PreparedTarget) -> dict[str, str]:
    merged = dict(env)
    merged.update({key: value for key, value in target.build_env.items() if key})
    tmp_root = default_tmp_root()
    tmp_root.mkdir(parents=True, exist_ok=True)
    merged.setdefault("TMPDIR", str(tmp_root))
    merged.setdefault("TMP", str(tmp_root))
    merged.setdefault("TEMP", str(tmp_root))
    merged.setdefault("SQLITE_TMPDIR", str(tmp_root))
    current_path = merged.get("PATH") or os.environ.get("PATH", "")
    if not shutil.which("mvn", path=current_path):
        maven_bin = _discover_maven_bin()
        if maven_bin:
            merged["PATH"] = f"{maven_bin}:{current_path}" if current_path else maven_bin
            merged.setdefault("MAVEN_HOME", str(Path(maven_bin).parent))
    return merged


def _run_build_command(
    cmd: list[str],
    *,
    log_path: Path,
    metadata_file: str | None,
    env_overrides: dict[str, str] | None = None,
) -> tuple[int, float]:
    env = dict(os.environ)
    if metadata_file:
        env["INDEX_METADATA_FILE"] = str(Path(metadata_file).resolve())
    if env_overrides:
        env.update(env_overrides)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    started = time.time()
    with log_path.open("w", encoding="utf-8") as log:
        log.write(f"# started_at={_now_iso()}\n")
        log.write("# command=" + " ".join(json.dumps(part, ensure_ascii=False) for part in cmd) + "\n\n")
        if env_overrides:
            log.write("# env_overrides=" + _json_dump(env_overrides).replace("\n", " ") + "\n\n")
        log.flush()
        proc = subprocess.Popen(
            cmd,
            cwd=str(HYBRID_ROOT),
            stdout=log,
            stderr=subprocess.STDOUT,
            env=env,
            text=True,
        )
        rc = proc.wait()
    return rc, time.time() - started


def _inspect_db(db_path: Path) -> tuple[dict[str, int], str, int, list[str]]:
    issues: list[str] = []
    counts: dict[str, int] = {}
    smoke_name = ""
    smoke_hit_count = 0

    if not db_path.is_file():
        return counts, smoke_name, smoke_hit_count, [f"missing_db:{db_path}"]

    conn = sqlite3.connect(str(db_path))
    try:
        for table in REQUIRED_DB_TABLES:
            try:
                row = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
            except sqlite3.DatabaseError as exc:
                issues.append(f"table_error:{table}:{exc}")
                continue
            count = int(row[0] if row is not None else 0)
            counts[table] = count
            if count <= 0:
                issues.append(f"empty_table:{table}")
        row = conn.execute(
            """
            SELECT display_name
            FROM symbols
            WHERE trim(display_name) != ''
              AND display_name != '<init>'
            ORDER BY length(display_name) DESC, display_name
            LIMIT 1
            """
        ).fetchone()
        smoke_name = str(row[0]).strip() if row and str(row[0]).strip() else ""
    finally:
        conn.close()

    if not smoke_name:
        issues.append("missing_smoke_symbol")
        return counts, smoke_name, smoke_hit_count, issues

    store = SqliteStore(str(db_path))
    try:
        hits = find_entity(store, type="any", name=smoke_name, match="exact", limit=3)
        smoke_hit_count = len(hits)
    finally:
        store.close()

    if smoke_hit_count <= 0:
        issues.append(f"smoke_find_entity_empty:{smoke_name}")
    return counts, smoke_name, smoke_hit_count, issues


def cmd_derive(args: argparse.Namespace) -> None:
    targets, samples = _load_targets_from_manifest_args(args)
    targets_doc = render_targets_doc(
        args.manifest,
        targets,
        worktrees_root=args.worktrees_root,
        index_output_dir=args.index_output_dir,
        metadata_file=args.metadata_file,
        config_path=args.config,
        overrides_path=args.overrides,
    )
    routes_doc = render_routes_doc(args.manifest, build_routes(samples, targets))

    targets_out = Path(args.targets_out) if args.targets_out else default_targets_path(args.manifest)
    routes_out = Path(args.routes_out) if args.routes_out else default_routes_path(args.manifest)
    _write_json(targets_out, targets_doc)
    _write_json(routes_out, routes_doc)
    print(
        _json_dump(
            {
                "ok": True,
                "targets_out": str(targets_out.resolve()),
                "routes_out": str(routes_out.resolve()),
                "unique_targets": targets_doc["summary"]["unique_targets"],
                "status_counts": targets_doc["summary"]["status_counts"],
                "pilot_targets": targets_doc["summary"]["pilot_targets"],
                "skipped_targets": targets_doc["summary"]["skipped_targets"],
            }
        )
    )


def cmd_build(args: argparse.Namespace) -> None:
    targets, samples = _load_targets_from_manifest_args(args)
    selected = _selected_targets(targets, args)
    index_output_dir = Path(args.index_output_dir) if args.index_output_dir else default_index_dir()
    logs_root = Path(args.logs_root) if args.logs_root else default_logs_root()
    batch_script = Path(args.batch_script) if args.batch_script else DEFAULT_BATCH_SCRIPT
    targets_out = Path(args.targets_out) if args.targets_out else default_targets_path(args.manifest)
    routes_out = Path(args.routes_out) if args.routes_out else default_routes_path(args.manifest)
    report_out = Path(args.report_out) if args.report_out else default_build_report_path(args.manifest)

    _ensure_parent_dirs(index_output_dir, logs_root, targets_out.parent, routes_out.parent, report_out.parent)

    results: list[dict[str, Any]] = []
    for target in selected:
        if target.skip:
            results.append(
                {
                    "slug": target.slug,
                    "repo": target.repo,
                    "base_sha": target.base_sha,
                    "status": "skipped_override",
                    "log_path": "",
                    "elapsed_s": 0.0,
                    "command": [],
                    "notes": target.notes,
                }
            )
            continue
        if bool(args.skip_ready) and target.reusable_index:
            results.append(
                {
                    "slug": target.slug,
                    "repo": target.repo,
                    "base_sha": target.base_sha,
                    "status": "skipped_ready",
                    "log_path": "",
                    "elapsed_s": 0.0,
                    "command": [],
                }
            )
            continue

        cmd = _build_command_for_target(target, batch_script=batch_script, index_output_dir=index_output_dir)
        log_path = logs_root / f"{target.slug}.log"
        env_overrides = _augment_env_for_build({}, target)
        if bool(args.dry_run):
            results.append(
                {
                    "slug": target.slug,
                    "repo": target.repo,
                    "base_sha": target.base_sha,
                    "status": "dry_run",
                    "log_path": str(log_path.resolve()),
                    "elapsed_s": 0.0,
                    "command": cmd,
                    "env_overrides": env_overrides,
                }
            )
            continue

        rc, elapsed_s = _run_build_command(
            cmd,
            log_path=log_path,
            metadata_file=args.metadata_file,
            env_overrides=env_overrides,
        )
        results.append(
            {
                "slug": target.slug,
                "repo": target.repo,
                "base_sha": target.base_sha,
                "status": "ok" if rc == 0 else "failed",
                "exit_code": rc,
                "log_path": str(log_path.resolve()),
                "elapsed_s": round(elapsed_s, 3),
                "command": cmd,
                "env_overrides": env_overrides,
            }
        )
        if rc != 0 and bool(args.stop_on_error):
            break

    refreshed_targets, refreshed_samples = _load_targets_from_manifest_args(args)
    targets_doc = render_targets_doc(
        args.manifest,
        refreshed_targets,
        worktrees_root=args.worktrees_root,
        index_output_dir=args.index_output_dir,
        metadata_file=args.metadata_file,
        config_path=args.config,
        overrides_path=args.overrides,
    )
    routes_doc = render_routes_doc(args.manifest, build_routes(refreshed_samples, refreshed_targets))
    _write_json(targets_out, targets_doc)
    _write_json(routes_out, routes_doc)

    summary_counts = Counter(result["status"] for result in results)
    report = {
        "meta": {
            "generated_at": _now_iso(),
            "manifest_path": str(Path(args.manifest).resolve()),
            "logs_root": str(logs_root.resolve()),
            "index_output_dir": str(index_output_dir.resolve()),
            "metadata_file": str(Path(args.metadata_file).resolve()) if args.metadata_file else "",
            "targets_out": str(targets_out.resolve()),
            "routes_out": str(routes_out.resolve()),
            "dry_run": bool(args.dry_run),
        },
        "summary": {
            "selected_targets": len(selected),
            "result_counts": dict(summary_counts),
        },
        "results": results,
    }
    _write_json(report_out, report)
    print(
        _json_dump(
            {
                "ok": True,
                "report_out": str(report_out.resolve()),
                "targets_out": str(targets_out.resolve()),
                "routes_out": str(routes_out.resolve()),
                "result_counts": dict(summary_counts),
            }
        )
    )
    if not bool(args.dry_run) and summary_counts.get("failed", 0) > 0:
        raise SystemExit(1)


def cmd_validate(args: argparse.Namespace) -> None:
    targets, samples = _load_targets_from_manifest_args(args)
    selected = _selected_targets(targets, args)
    targets_out = Path(args.targets_out) if args.targets_out else default_targets_path(args.manifest)
    routes_out = Path(args.routes_out) if args.routes_out else default_routes_path(args.manifest)
    report_out = Path(args.report_out) if args.report_out else default_validation_report_path(args.manifest)
    _ensure_parent_dirs(targets_out.parent, routes_out.parent, report_out.parent)

    results: list[dict[str, Any]] = []
    for target in selected:
        worktree = Path(target.worktree_path)
        counts, smoke_name, smoke_hit_count, issues = _inspect_db(Path(target.effective_db_path))
        worktree_exists = worktree.is_dir()
        if bool(args.require_worktree) and not worktree_exists:
            issues.append(f"missing_worktree:{worktree}")
        results.append(
            {
                "slug": target.slug,
                "repo": target.repo,
                "base_sha": target.base_sha,
                "index_status": target.index_status,
                "metadata_status": target.metadata_status,
                "db_path": target.effective_db_path,
                "db_exists": Path(target.effective_db_path).is_file(),
                "worktree_path": target.worktree_path,
                "worktree_exists": worktree_exists,
                "counts": counts,
                "smoke_symbol": smoke_name,
                "smoke_hit_count": smoke_hit_count,
                "ok": not issues,
                "issues": issues,
            }
        )

    targets_doc = render_targets_doc(
        args.manifest,
        targets,
        worktrees_root=args.worktrees_root,
        index_output_dir=args.index_output_dir,
        metadata_file=args.metadata_file,
        config_path=args.config,
        overrides_path=args.overrides,
    )
    routes_doc = render_routes_doc(args.manifest, build_routes(samples, targets))
    _write_json(targets_out, targets_doc)
    _write_json(routes_out, routes_doc)

    summary_counts = Counter("ok" if result["ok"] else "failed" for result in results)
    report = {
        "meta": {
            "generated_at": _now_iso(),
            "manifest_path": str(Path(args.manifest).resolve()),
            "require_worktree": bool(args.require_worktree),
            "targets_out": str(targets_out.resolve()),
            "routes_out": str(routes_out.resolve()),
        },
        "summary": {
            "validated_targets": len(selected),
            "result_counts": dict(summary_counts),
        },
        "results": results,
    }
    _write_json(report_out, report)
    print(
        _json_dump(
            {
                "ok": True,
                "report_out": str(report_out.resolve()),
                "targets_out": str(targets_out.resolve()),
                "routes_out": str(routes_out.resolve()),
                "result_counts": dict(summary_counts),
            }
        )
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Prepare Java eval worktrees and indexes from a JSONL manifest")
    sub = parser.add_subparsers(dest="command", required=True)

    def add_common_args(p: argparse.ArgumentParser) -> None:
        p.add_argument("--manifest", required=True, help="JSONL manifest path")
        p.add_argument("--worktrees-root", default="", help="源码工作树根目录")
        p.add_argument("--index-output-dir", default="", help="SQLite 索引输出目录")
        p.add_argument("--metadata-file", default="", help="index_metadata.json 路径")
        p.add_argument("--config", default="", help="默认 hybrid 配置文件路径")
        p.add_argument("--overrides", default="", help="可选：repo+sha 覆盖参数 JSON")
        p.add_argument("--targets-out", default="", help="输出 targets JSON；默认写入 var/java_eval/manifests")
        p.add_argument("--routes-out", default="", help="输出 sample routes JSON；默认写入 var/java_eval/manifests")

    derive = sub.add_parser("derive", help="从 manifest 生成唯一目标清单与样本路由")
    add_common_args(derive)
    derive.set_defaults(func=cmd_derive)

    build = sub.add_parser("build", help="批量 clone + 建索引（复用 repo_commit_to_index.sh）")
    add_common_args(build)
    build.add_argument("--logs-root", default="", help="构建日志目录")
    build.add_argument("--report-out", default="", help="构建报告 JSON 输出路径")
    build.add_argument("--batch-script", default="", help="默认 scripts/repo_commit_to_index.sh")
    build.add_argument("--sample-id", action="append", default=[], help="仅构建包含该 sample_id 的目标")
    build.add_argument("--slug", action="append", default=[], help="仅构建指定 slug")
    build.add_argument("--repo", action="append", default=[], help="仅构建指定 repo")
    build.add_argument("--limit", type=int, default=0, help="最多处理 N 个目标")
    build.add_argument("--only-pilot", action="store_true", help="仅处理 override 中标记 pilot=true 的目标")
    build.add_argument("--skip-ready", action=argparse.BooleanOptionalAction, default=True, help="跳过已 ready 的索引")
    build.add_argument("--dry-run", action="store_true", help="只生成命令与报告，不真正执行")
    build.add_argument("--stop-on-error", action="store_true", help="遇到失败立即停止")
    build.set_defaults(func=cmd_build)

    validate = sub.add_parser("validate", help="校验索引存在性、表计数和最小 find_entity smoke")
    add_common_args(validate)
    validate.add_argument("--report-out", default="", help="校验报告 JSON 输出路径")
    validate.add_argument("--sample-id", action="append", default=[], help="仅校验包含该 sample_id 的目标")
    validate.add_argument("--slug", action="append", default=[], help="仅校验指定 slug")
    validate.add_argument("--repo", action="append", default=[], help="仅校验指定 repo")
    validate.add_argument("--limit", type=int, default=0, help="最多校验 N 个目标")
    validate.add_argument("--only-pilot", action="store_true", help="仅校验 override 中标记 pilot=true 的目标")
    validate.add_argument(
        "--require-worktree",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="同时要求源码工作树存在",
    )
    validate.set_defaults(func=cmd_validate)
    return parser


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
