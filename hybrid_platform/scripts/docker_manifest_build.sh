#!/usr/bin/env bash
# 批量流程：derive manifest -> clone target repo -> Docker 内 scip-java 生成 index.scip
# -> 宿主机 ingest/build-code-graph/chunk/embed -> metadata_upsert
#
# 默认只处理 targets.json 中 index_status == missing 的目标。
#
# 依赖：
#   - Docker 可用
#   - hybrid_platform/myenv 可用
#   - Docker 镜像允许访问外网依赖（Maven/Gradle）
#
# 示例：
#   ./scripts/docker_manifest_build.sh \
#     --manifest "/data1/qadong/codeindex_java/JAVA test/test_java_agent_manifest_size_ge_100000.jsonl" \
#     --config ./var/server_vllm_generic_config.json \
#     --overrides ./var/java_eval_overrides.json

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HYBRID_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
PYTHON="${HYBRID_PYTHON:-${HYBRID_ROOT}/myenv/bin/python}"

usage() {
  cat >&2 <<'EOF'
docker_manifest_build.sh — 批量 Docker 产 SCIP，宿主机续跑后半段索引

必填：
  --manifest PATH
  --config PATH

可选：
  --overrides PATH
  --targets-json PATH       目标清单；默认 var/java_eval/manifests/<manifest>.targets.json
  --logs-root PATH          默认 var/java_eval/logs-docker
  --index-output-dir PATH   默认 var/hybrid_indices
  --only-status STATUS      默认 missing；可选 missing/db_only/ready/all
  --slug SLUG               只跑指定 slug（可重复）
  --limit N                 最多处理 N 个目标
  --build-images            先构建 jdk11/jdk17/jdk21/jdk23 镜像
  --skip-derive             不重新 derive，直接使用现有 targets.json
  --dry-run                 只打印将要执行的目标

说明：
  - 目标 JDK 依据 targets.json 的 java_home 选择镜像：
      jdk-11 -> hybrid-scip-build:jdk11
      jdk-17 -> hybrid-scip-build:jdk17
      jdk-21 -> hybrid-scip-build:jdk21
      jdk-23 -> hybrid-scip-build:jdk23
  - jdk23 镜像默认使用 eclipse-temurin:23-jdk（不是 23-jdk-jammy）
  - overrides 可对目标设置 "skip": true（可配合 "notes"）；该目标会在批量中直接跳过并打印 [skip]
EOF
}

MANIFEST=""
CONFIG_PATH=""
OVERRIDES_PATH=""
TARGETS_JSON=""
LOGS_ROOT=""
INDEX_OUTPUT_DIR=""
ONLY_STATUS="missing"
BUILD_IMAGES=0
SKIP_DERIVE=0
DRY_RUN=0
LIMIT=0
SLUGS=()

while [[ $# -gt 0 ]]; do
  case "$1" in
    --manifest) MANIFEST="${2:?}"; shift 2 ;;
    --config) CONFIG_PATH="${2:?}"; shift 2 ;;
    --overrides) OVERRIDES_PATH="${2:?}"; shift 2 ;;
    --targets-json) TARGETS_JSON="${2:?}"; shift 2 ;;
    --logs-root) LOGS_ROOT="${2:?}"; shift 2 ;;
    --index-output-dir) INDEX_OUTPUT_DIR="${2:?}"; shift 2 ;;
    --only-status) ONLY_STATUS="${2:?}"; shift 2 ;;
    --slug) SLUGS+=("${2:?}"); shift 2 ;;
    --limit) LIMIT="${2:?}"; shift 2 ;;
    --build-images) BUILD_IMAGES=1; shift ;;
    --skip-derive) SKIP_DERIVE=1; shift ;;
    --dry-run) DRY_RUN=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *)
      echo "Unknown option: $1" >&2
      exit 2
      ;;
  esac
done

if [[ -z "$MANIFEST" || -z "$CONFIG_PATH" ]]; then
  usage
  exit 2
fi

REPO_ROOT="$(cd "$HYBRID_ROOT/.." && pwd)"
WORKSPACE="${REPO_ROOT}/workspace"

TARGETS_JSON="${TARGETS_JSON:-${WORKSPACE}/manifests/$(basename "${MANIFEST%.*}").targets.json}"
LOGS_ROOT="${LOGS_ROOT:-${WORKSPACE}/logs}"
INDEX_OUTPUT_DIR="${INDEX_OUTPUT_DIR:-${WORKSPACE}/indices}"

mkdir -p "${WORKSPACE}/cache/tmp" "${WORKSPACE}/cache/m2" "${WORKSPACE}/cache/gradle" \
  "$LOGS_ROOT" "$INDEX_OUTPUT_DIR" "${WORKSPACE}/manifests" "${WORKSPACE}/worktrees"

export INDEX_METADATA_FILE="${INDEX_METADATA_FILE:-${WORKSPACE}/index_metadata.json}"
if [[ ! -f "$INDEX_METADATA_FILE" ]]; then
  printf '{"version":1,"entries":[]}\n' > "$INDEX_METADATA_FILE"
fi

if [[ "$BUILD_IMAGES" -eq 1 ]]; then
  cd "${HYBRID_ROOT}/docker/scip-build"
  declare -A BASE_IMAGES=(
    [11]="eclipse-temurin:11-jdk-jammy"
    [17]="eclipse-temurin:17-jdk-jammy"
    [21]="eclipse-temurin:21-jdk-jammy"
    [23]="eclipse-temurin:23-jdk"
  )
  for v in 11 17 21 23; do
    echo "=== build docker image hybrid-scip-build:jdk${v} ==="
    docker build \
      --build-arg "BASE_IMAGE=${BASE_IMAGES[$v]}" \
      -t "hybrid-scip-build:jdk${v}" \
      -f Dockerfile .
  done
fi

cd "$HYBRID_ROOT"

if [[ "$SKIP_DERIVE" -eq 0 ]]; then
  DERIVE_CMD=(./scripts/java_eval_index_prep.sh derive --manifest "$MANIFEST" --config "$CONFIG_PATH")
  if [[ -n "$OVERRIDES_PATH" ]]; then
    DERIVE_CMD+=(--overrides "$OVERRIDES_PATH")
  fi
  "${DERIVE_CMD[@]}"
fi

SLUG_JSON="[]"
if [[ ${#SLUGS[@]} -gt 0 ]]; then
  SLUG_JSON="$("$PYTHON" -c 'import json, sys; print(json.dumps(sys.argv[1:]))' "${SLUGS[@]}")"
fi

export HYBRID_ROOT MANIFEST CONFIG_PATH OVERRIDES_PATH TARGETS_JSON LOGS_ROOT INDEX_OUTPUT_DIR ONLY_STATUS DRY_RUN LIMIT SLUG_JSON
export HYBRID_PYTHON="$PYTHON"

"$PYTHON" - <<'PY'
from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
from pathlib import Path

hybrid_root = Path(os.environ["HYBRID_ROOT"])
targets_json = Path(os.environ["TARGETS_JSON"])
logs_root = Path(os.environ["LOGS_ROOT"])
index_output_dir = Path(os.environ["INDEX_OUTPUT_DIR"])
only_status = os.environ["ONLY_STATUS"].strip()
dry_run = os.environ["DRY_RUN"] == "1"
limit = int(os.environ["LIMIT"] or "0")
slug_filter = set(json.loads(os.environ["SLUG_JSON"]))

data = json.loads(targets_json.read_text(encoding="utf-8"))
targets = data["targets"]

def choose_image(java_home: str) -> str:
    s = (java_home or "").lower()
    if "jdk-23" in s or "jdk23" in s:
        return "hybrid-scip-build:jdk23"
    if "jdk-21" in s or "jdk21" in s:
        return "hybrid-scip-build:jdk21"
    if "jdk-17" in s or "jdk17" in s:
        return "hybrid-scip-build:jdk17"
    if "jdk-11" in s or "jdk11" in s:
        return "hybrid-scip-build:jdk11"
    return "hybrid-scip-build:jdk17"

selected = []
skipped = []
for target in targets:
    slug = target["slug"]
    if slug_filter and slug not in slug_filter:
        continue
    if bool(target.get("skip", False)):
        skipped.append({"slug": slug, "notes": str(target.get("notes", "")).strip()})
        continue
    status = target.get("index_status", "")
    if only_status != "all" and status != only_status:
        continue
    selected.append(target)
    if limit > 0 and len(selected) >= limit:
        break

print(
    json.dumps(
        {
            "selected_targets": len(selected),
            "skipped_targets": len(skipped),
            "only_status": only_status,
            "dry_run": dry_run,
        },
        ensure_ascii=False,
    )
)
for item in skipped:
    note = item["notes"]
    if note:
        print(f"[skip] {item['slug']} note={note}", file=sys.stderr)
    else:
        print(f"[skip] {item['slug']} note=skip override", file=sys.stderr)

for i, target in enumerate(selected, 1):
    slug = target["slug"]
    repo = target["repo"]
    repo_url = target["repo_url"]
    commit = target["base_sha"]
    worktree = Path(target["worktree_path"])
    scip_path = worktree / "index.scip"
    log_path = logs_root / f"{slug}.log"
    image = choose_image(target.get("java_home", ""))
    build_tool = (target.get("build_tool") or "").strip()
    build_env = dict(target.get("build_env") or {})
    docker_packages = target.get("docker_packages") or []
    docker_pre_script = str(target.get("docker_pre_script") or "").strip()

    env = dict(os.environ)
    env.update(build_env)
    env.setdefault("TMPDIR", "/data1/qadong/tmp")
    env.setdefault("TMP", "/data1/qadong/tmp")
    env.setdefault("TEMP", "/data1/qadong/tmp")
    env.setdefault("SQLITE_TMPDIR", "/data1/qadong/tmp")
    env.setdefault("ARROW_TMPDIR", env["TMPDIR"])
    env.setdefault("MAVEN_REPO_LOCAL", "/data1/qadong/.m2/repository")
    env.setdefault("GRADLE_USER_HOME", "/data1/qadong/.gradle")
    env.setdefault("JAVA_TOOL_OPTIONS", f"-Djava.io.tmpdir={env['TMPDIR']}")
    if "maven.repo.local" not in env.get("MAVEN_OPTS", ""):
        extra = f"-Dmaven.repo.local={env['MAVEN_REPO_LOCAL']}"
        env["MAVEN_OPTS"] = (env.get("MAVEN_OPTS", "") + " " + extra).strip()

    clone_cmd = [
        "bash", str(hybrid_root / "scripts/clone_repo_at_commit.sh"),
        "--git-url", repo_url,
        "--commit", commit,
        "--dest", str(worktree),
    ]
    if target.get("recurse_submodules"):
        clone_cmd.append("--recurse-submodules")
    if target.get("clone_shallow"):
        clone_cmd.append("--shallow")

    docker_cmd = [
        "bash", str(hybrid_root / "docker/scip-build/docker_scip_build.sh"),
        str(worktree),
        str(scip_path),
        "--",
    ]
    if build_tool:
        docker_cmd.extend(["--build-tool", build_tool])
    docker_cmd.extend(target.get("build_args") or [])

    host_cmd = [
        "bash", str(hybrid_root / "scripts/repo_commit_to_index.sh"),
        "--config", target["config_path"],
        "--repo-name", repo,
        "--commit", commit,
        "--dest", str(worktree),
        "--skip-clone",
        "--output-dir", str(index_output_dir),
        "--prebuilt-scip", str(scip_path),
    ]

    print(f"[{i}/{len(selected)}] {slug} image={image}")
    if dry_run:
        for name, cmd in (("clone", clone_cmd), ("docker-scip", docker_cmd), ("host-index", host_cmd)):
            print(name + ":", " ".join(shlex.quote(x) for x in cmd))
        continue

    # ── worktree 健康检查：自动修复损坏状态，避免 clone/build 莫名失败 ──
    if worktree.exists():
        _healthy = True
        try:
            _rc = subprocess.run(
                ["git", "-C", str(worktree), "status", "--porcelain"],
                capture_output=True, timeout=10,
            ).returncode
            if _rc != 0:
                _healthy = False
        except Exception:
            _healthy = False
        if not _healthy:
            import shutil
            print(f"  [cleanup] removing corrupted worktree {worktree}", file=sys.stderr)
            shutil.rmtree(str(worktree), ignore_errors=True)
        else:
            # remove broken symlinks that can trip up plugins
            for _root, _dirs, _files in os.walk(str(worktree)):
                for _name in _files:
                    _p = os.path.join(_root, _name)
                    if os.path.islink(_p) and not os.path.exists(_p):
                        os.unlink(_p)

    env["SCIP_BUILD_IMAGE"] = image
    if docker_packages:
        env["DOCKER_APT_PACKAGES"] = " ".join(docker_packages)
    else:
        env.pop("DOCKER_APT_PACKAGES", None)
    if docker_pre_script:
        env["DOCKER_PRE_SCRIPT"] = docker_pre_script
    else:
        env.pop("DOCKER_PRE_SCRIPT", None)
    if build_env:
        env["DOCKER_EXTRA_ENV"] = ",".join(f"{k}={v}" for k, v in build_env.items() if k)
    else:
        env.pop("DOCKER_EXTRA_ENV", None)
    logs_root.mkdir(parents=True, exist_ok=True)
    with log_path.open("w", encoding="utf-8") as log:
        for name, cmd in (("clone", clone_cmd), ("docker-scip", docker_cmd), ("host-index", host_cmd)):
            log.write(f"\n### {name}\n")
            log.write("$ " + " ".join(shlex.quote(x) for x in cmd) + "\n")
            log.flush()
            proc = subprocess.run(
                cmd,
                cwd=str(hybrid_root),
                env=env,
                stdout=log,
                stderr=subprocess.STDOUT,
                text=True,
            )
            if proc.returncode != 0:
                print(f"[{i}/{len(selected)}] FAIL {slug} step={name} log={log_path}", file=sys.stderr)
                break
        else:
            print(f"[{i}/{len(selected)}] OK {slug}")
PY
