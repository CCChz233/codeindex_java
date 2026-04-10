#!/usr/bin/env bash
# Git clone（可选）→ index-java → build-code-graph → chunk → embed，并写入 var/index_metadata.json。
# MCP 不在此脚本启动；统一入口见 scripts/start_mcp_gateway_8765.sh（单端口 Nginx 反代多后端）。
#
# 环境变量：INDEX_METADATA_FILE — 非默认时可指向其它 metadata 路径
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HYBRID_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
PYTHON="${HYBRID_PYTHON:-${HYBRID_ROOT}/myenv/bin/python}"

usage() {
  cat >&2 <<'EOF'
repo_commit_to_index.sh — clone → 全量索引 → 注册 index_metadata.json

阶段：clone_git → index-java → build-code-graph → chunk → embed → metadata_upsert

必填：--config --repo-name --commit --dest
未加 --skip-clone 时还需：--git-url

可选：--output-dir --build-tool --java-home --prebuilt-scip PATH --recurse-submodules --clone-shallow --skip-clone
Maven/Gradle 参数：-- 之后（Gradle 慎用 -x test，见文档）；若指定 --prebuilt-scip 则不再调用宿主机 scip-java

完成后请执行：./scripts/start_mcp_gateway_8765.sh
EOF
}

CONFIG_CLI=""
REPO_NAME_CLI=""
COMMIT_CLI=""
DEST_CLI=""
GIT_URL_CLI=""
OUTPUT_DIR_CLI=""
BUILD_TOOL_CLI=""
JAVA_HOME_CLI=""
PREBUILT_SCIP_CLI=""
SKIP_CLONE=0
RECURSE=0
CLONE_SHALLOW=0
EXTRA_JAVA=()

while [[ $# -gt 0 ]]; do
  case "$1" in
    --config) CONFIG_CLI="${2:?}"; shift 2 ;;
    --repo-name|--repo) REPO_NAME_CLI="${2:?}"; shift 2 ;;
    --commit) COMMIT_CLI="${2:?}"; shift 2 ;;
    --dest) DEST_CLI="${2:?}"; shift 2 ;;
    --git-url) GIT_URL_CLI="${2:?}"; shift 2 ;;
    --output-dir) OUTPUT_DIR_CLI="${2:?}"; shift 2 ;;
    --build-tool) BUILD_TOOL_CLI="${2:?}"; shift 2 ;;
    --java-home) JAVA_HOME_CLI="${2:?}"; shift 2 ;;
    --prebuilt-scip) PREBUILT_SCIP_CLI="${2:?}"; shift 2 ;;
    --recurse-submodules) RECURSE=1; shift ;;
    --clone-shallow) CLONE_SHALLOW=1; shift ;;
    --skip-clone) SKIP_CLONE=1; shift ;;
    -h|--help) usage; exit 0 ;;
    --) shift; EXTRA_JAVA=("$@"); break ;;
    *)
      echo "Unknown option: $1 (try --help)" >&2
      exit 2
      ;;
  esac
done

CONFIG_PATH="${CONFIG_CLI:-${CONFIG_PATH:-${HYBRID_CONFIG_PATH:-}}}"
REPO_NAME="${REPO_NAME_CLI:-${REPO_NAME:-${HYBRID_REPO_NAME:-}}}"
COMMIT_SHA="${COMMIT_CLI:-${COMMIT_SHA:-${HYBRID_COMMIT_SHA:-}}}"
DEST="${DEST_CLI:-${DEST:-${REPO_ROOT:-}}}"
GIT_URL="${GIT_URL_CLI:-${GIT_URL:-}}"
OUTPUT_DIR="${OUTPUT_DIR_CLI:-${OUTPUT_DIR:-${HYBRID_INDEX_DIR:-$HYBRID_ROOT/var/hybrid_indices}}}"
META_OPT=()
if [[ -n "${INDEX_METADATA_FILE:-}" ]]; then
  META_OPT=(--metadata-file "$INDEX_METADATA_FILE")
fi

if [[ -z "$CONFIG_PATH" || -z "$REPO_NAME" || -z "$COMMIT_SHA" || -z "$DEST" ]]; then
  echo "Missing: --config, --repo-name, --commit, --dest" >&2
  exit 2
fi
if [[ "$SKIP_CLONE" -eq 0 && -z "$GIT_URL" ]]; then
  echo "Missing: --git-url (or use --skip-clone)" >&2
  exit 2
fi

COMMIT_SHA="$(printf '%s' "$COMMIT_SHA" | tr '[:upper:]' '[:lower:]')"

if [[ "$SKIP_CLONE" -eq 0 ]]; then
  echo ">>> PIPELINE_STAGE_START: clone_git" >&2
  CLONE_ARGS=( "$SCRIPT_DIR/clone_repo_at_commit.sh" --git-url "$GIT_URL" --commit "$COMMIT_SHA" --dest "$DEST" )
  [[ "$RECURSE" -eq 1 ]] && CLONE_ARGS+=(--recurse-submodules)
  [[ "$CLONE_SHALLOW" -eq 1 ]] && CLONE_ARGS+=(--shallow)
  set +e
  bash "${CLONE_ARGS[@]}"
  rc=$?
  set -e
  if [[ "$rc" -ne 0 ]]; then
    echo ">>> PIPELINE_STAGE_FAILED: clone_git exit_code=$rc" >&2
    exit "$rc"
  fi
  echo ">>> PIPELINE_STAGE_OK: clone_git" >&2
else
  echo ">>> PIPELINE_STAGE_SKIP: clone_git (--skip-clone)" >&2
fi

BUILD_CMD=(bash "$SCRIPT_DIR/index_build_repo_commit.sh" --config "$CONFIG_PATH"
  --repo-name "$REPO_NAME" --commit "$COMMIT_SHA" --repo-root "$DEST")
[[ -n "$OUTPUT_DIR" ]] && BUILD_CMD+=(--output-dir "$OUTPUT_DIR")
[[ -n "$BUILD_TOOL_CLI" ]] && BUILD_CMD+=(--build-tool "$BUILD_TOOL_CLI")
[[ -n "$JAVA_HOME_CLI" ]] && BUILD_CMD+=(--java-home "$JAVA_HOME_CLI")
[[ -n "$PREBUILT_SCIP_CLI" ]] && BUILD_CMD+=(--prebuilt-scip "$PREBUILT_SCIP_CLI")
if [[ ${#EXTRA_JAVA[@]} -gt 0 ]]; then
  BUILD_CMD+=(--)
  BUILD_CMD+=("${EXTRA_JAVA[@]}")
fi

if [[ -n "${PREBUILT_SCIP_CLI:-}" ]]; then
  echo ">>> PIPELINE_PHASE: index_build (ingest from prebuilt-scip → build-code-graph → chunk → embed)" >&2
else
  echo ">>> PIPELINE_PHASE: index_build (index-java → build-code-graph → chunk → embed)" >&2
fi
set +e
"${BUILD_CMD[@]}"
rc=$?
set -e
if [[ "$rc" -ne 0 ]]; then
  echo ">>> PIPELINE_STAGE_FAILED: index_build exit_code=$rc" >&2
  exit "$rc"
fi

echo ">>> PIPELINE_STAGE_START: metadata_upsert" >&2
UPSERT_ARGS=(
  -m hybrid_platform.index_metadata upsert
  --repo "$REPO_NAME"
  --commit "$COMMIT_SHA"
  --config "$CONFIG_PATH"
  --output-dir "$OUTPUT_DIR"
)
UPSERT_ARGS+=("${META_OPT[@]}")
(cd "$HYBRID_ROOT" && "$PYTHON" "${UPSERT_ARGS[@]}")
echo ">>> PIPELINE_STAGE_OK: metadata_upsert" >&2
echo ">>> PIPELINE_DONE: index + metadata. Start gateway: $SCRIPT_DIR/start_mcp_gateway_8765.sh" >&2
