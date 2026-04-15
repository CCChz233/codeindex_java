#!/usr/bin/env bash
# 一键：Java build-java-index（正式入口）或 prebuilt SCIP → ingest → build-code-graph → chunk → embed。
# 数据库：${OUTPUT_DIR}/${slug}.db，slug = sanitize(repo)_${commit_sha}（commit 小写 hex）。
#
# 参数（命令行优先于环境变量；环境变量仍可用 CONFIG_PATH / REPO_NAME / COMMIT_SHA / REPO_ROOT 等）：
#   --config PATH       配置文件（同 cli 全局 --config）
#   --repo-name NAME    逻辑仓库名（与 index-java --repo 一致）
#   --commit SHA        Git commit（hex，脚本内转小写）
#   --repo-root PATH    已 checkout 到该 commit 的源码根
#   --output-dir PATH   存放 *.db，默认 <仓库>/var/hybrid_indices
#   --build-tool TOOL   maven 或 gradle
#   --java-home PATH    设置 JAVA_HOME，并把 $JAVA_HOME/bin prepend 到 PATH（Maven/Gradle/scip-java 用此 JDK）
#   --prebuilt-scip PATH  跳过 index-java，直接 ingest 该 .scip（Docker 内编译产出后宿主机续跑）
#   -h, --help
#
# 环境变量备选：HYBRID_CONFIG_PATH、HYBRID_REPO_NAME、HYBRID_COMMIT_SHA、HYBRID_REPO_ROOT、
#   HYBRID_INDEX_DIR、BUILD_TOOL、HYBRID_PREBUILT_SCIP、SKIP_CHUNK、SKIP_EMBED、SKIP_CODE_GRAPH
#
# index-java 的编译参数放在单独的 -- 之后：
#   ./scripts/index_build_repo_commit.sh --config ./config/default_config.json \
#     --repo-name spring-projects/spring-framework --commit abcdef... \
#     --repo-root /path/to/spring-framework --build-tool maven -- -DskipTests
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HYBRID_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
PYTHON="${HYBRID_PYTHON:-${HYBRID_ROOT}/myenv/bin/python}"

usage() {
  cat >&2 <<'EOF'
index_build_repo_commit.sh — Java 一键构建索引（build-java-index，或 prebuilt SCIP → ingest → build-code-graph → chunk → embed）

必填参数（或同名环境变量；命令行优先）：
  --config PATH        hybrid JSON 配置
  --repo-name NAME     逻辑仓库名（与 index-java --repo 一致）
  --commit SHA         Git commit（hex，脚本内转小写）
  --repo-root PATH     已 checkout 的源码根

可选：
  --output-dir PATH    存放 *.db（默认 hybrid_platform/var/hybrid_indices）
  --build-tool TOOL    maven 或 gradle
  --java-home PATH     指定 JDK（如 Java 21：/usr/lib/jvm/java-21-openjdk-amd64）；等价于事先 export JAVA_HOME
  --prebuilt-scip PATH 已有 index.scip 时跳过 scip-java，仅跑 ingest 及后续（也可用 HYBRID_PREBUILT_SCIP）

环境变量备选：CONFIG_PATH、REPO_NAME、COMMIT_SHA、REPO_ROOT、OUTPUT_DIR、HYBRID_INDEX_DIR、
  BUILD_TOOL、JAVA_HOME、HYBRID_PREBUILT_SCIP、SKIP_CHUNK、SKIP_EMBED、SKIP_CODE_GRAPH

index-java 编译参数放在单独的 -- 之后，例如：-- -DskipTests

示例：
  ./scripts/index_build_repo_commit.sh --config ./config/default_config.json \
    --repo-name spring-projects/spring-framework --commit abcdef0123456789abcdef0123456789abcdef01 \
    --repo-root /path/to/spring-framework --build-tool maven -- -DskipTests

  ./scripts/index_build_repo_commit.sh --config ./config/default_config.json \
    --repo-name my/repo --commit abcdef0123456789abcdef0123456789abcdef01 \
    --repo-root /path/to/repo --prebuilt-scip /path/to/index.scip
EOF
}

CONFIG_CLI=""
REPO_NAME_CLI=""
COMMIT_CLI=""
REPO_ROOT_CLI=""
OUTPUT_DIR_CLI=""
BUILD_TOOL_CLI=""
JAVA_HOME_CLI=""
PREBUILT_SCIP_CLI=""

EXTRA_JAVA=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    --config)
      CONFIG_CLI="${2:?--config requires a path}"
      shift 2
      ;;
    --repo-name|--repo)
      REPO_NAME_CLI="${2:?--repo-name requires a value}"
      shift 2
      ;;
    --commit)
      COMMIT_CLI="${2:?--commit requires a value}"
      shift 2
      ;;
    --repo-root)
      REPO_ROOT_CLI="${2:?--repo-root requires a path}"
      shift 2
      ;;
    --output-dir)
      OUTPUT_DIR_CLI="${2:?--output-dir requires a path}"
      shift 2
      ;;
    --build-tool)
      BUILD_TOOL_CLI="${2:?--build-tool requires maven or gradle}"
      shift 2
      ;;
    --java-home)
      JAVA_HOME_CLI="${2:?--java-home requires a path}"
      shift 2
      ;;
    --prebuilt-scip)
      PREBUILT_SCIP_CLI="${2:?--prebuilt-scip requires a path}"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    --)
      shift
      EXTRA_JAVA=("$@")
      break
      ;;
    *)
      echo "Unknown option: $1 (try --help; use -- before index-java build args)" >&2
      exit 2
      ;;
  esac
done

CONFIG_PATH="${CONFIG_CLI:-${CONFIG_PATH:-${HYBRID_CONFIG_PATH:-}}}"
REPO_NAME="${REPO_NAME_CLI:-${REPO_NAME:-${HYBRID_REPO_NAME:-}}}"
COMMIT_SHA="${COMMIT_CLI:-${COMMIT_SHA:-${HYBRID_COMMIT_SHA:-}}}"
REPO_ROOT="${REPO_ROOT_CLI:-${REPO_ROOT:-${HYBRID_REPO_ROOT:-}}}"
OUTPUT_DIR="${OUTPUT_DIR_CLI:-${OUTPUT_DIR:-${HYBRID_INDEX_DIR:-$HYBRID_ROOT/var/hybrid_indices}}}"
if [[ -n "${BUILD_TOOL_CLI}" ]]; then
  BUILD_TOOL="$BUILD_TOOL_CLI"
fi

PREBUILT_SCIP="${PREBUILT_SCIP_CLI:-${HYBRID_PREBUILT_SCIP:-}}"
if [[ -n "$PREBUILT_SCIP" ]] && [[ ${#EXTRA_JAVA[@]} -gt 0 ]]; then
  echo "[index_build_repo_commit] WARN: --prebuilt-scip 已设置，忽略 index-java 的额外编译参数（-- 之后）" >&2
fi

# scip-java / Maven / Gradle 要求 JAVA_HOME 指向「含 bin/java」的 JDK 根目录；无效路径会导致子进程报 invalid directory
_jdk_home_from_path_java() {
  local j
  j=$(command -v java 2>/dev/null) || return 1
  if command -v readlink >/dev/null 2>&1; then
    j=$(readlink -f "$j" 2>/dev/null || readlink "$j" 2>/dev/null || echo "$j")
  fi
  local home
  home=$(cd "$(dirname "$j")/.." && pwd)
  [[ -x "$home/bin/java" ]] && echo "$home" && return 0
  return 1
}

JH="${JAVA_HOME_CLI:-${JAVA_HOME:-}}"
if [[ -z "$PREBUILT_SCIP" ]]; then
if [[ -n "$JH" ]]; then
  export JAVA_HOME="$JH"
  export PATH="$JAVA_HOME/bin:$PATH"
fi
if [[ -n "$JH" ]] && [[ ! -x "$JAVA_HOME/bin/java" ]]; then
  echo "[index_build_repo_commit] ERROR: JAVA_HOME is invalid: no executable at $JAVA_HOME/bin/java" >&2
  if ALT="$(_jdk_home_from_path_java)" && [[ -n "$ALT" ]]; then
    echo "[index_build_repo_commit] Falling back to JDK inferred from PATH java: $ALT" >&2
    export JAVA_HOME="$ALT"
    export PATH="$JAVA_HOME/bin:$PATH"
  else
    echo "  Fix: install JDK 21, or set --java-home to the directory that contains bin/java." >&2
    echo "  Example: dirname \"\$(dirname \"\$(readlink -f \"\$(command -v java)\")\")\")" >&2
    exit 2
  fi
fi
if [[ -n "${JAVA_HOME:-}" ]]; then
  echo "[index_build_repo_commit] JAVA_HOME=$JAVA_HOME java=$(command -v java || true) ($("$JAVA_HOME/bin/java" -version 2>&1 | head -1 || true))" >&2
fi
else
  echo "[index_build_repo_commit] prebuilt-scip mode: skip host JAVA_HOME check (ingest/chunk/embed 仅需 Python)" >&2
fi

if [[ -z "$CONFIG_PATH" || -z "$REPO_NAME" || -z "$COMMIT_SHA" || -z "$REPO_ROOT" ]]; then
  echo "Missing required: --config, --repo-name, --commit, --repo-root (or set env CONFIG_PATH, REPO_NAME, COMMIT_SHA, REPO_ROOT)." >&2
  echo "Run with --help for usage." >&2
  exit 2
fi

COMMIT_SHA="$(printf '%s' "$COMMIT_SHA" | tr '[:upper:]' '[:lower:]')"
mkdir -p "$OUTPUT_DIR"

TMP_ROOT="${TMPDIR:-${TMP:-${TEMP:-/data1/qadong/tmp}}}"
mkdir -p "$TMP_ROOT"
export TMPDIR="${TMPDIR:-$TMP_ROOT}"
export TMP="${TMP:-$TMP_ROOT}"
export TEMP="${TEMP:-$TMP_ROOT}"
export SQLITE_TMPDIR="${SQLITE_TMPDIR:-$TMP_ROOT}"
export ARROW_TMPDIR="${ARROW_TMPDIR:-$TMP_ROOT}"
echo "[index_build_repo_commit] TMPDIR=$TMPDIR SQLITE_TMPDIR=$SQLITE_TMPDIR ARROW_TMPDIR=$ARROW_TMPDIR" >&2

export HYBRID_REPO_NAME="$REPO_NAME"
export HYBRID_COMMIT_SHA="$COMMIT_SHA"
SLUG="$("$PYTHON" -c "import os; from hybrid_platform.index_slug import repo_commit_slug; print(repo_commit_slug(os.environ['HYBRID_REPO_NAME'], os.environ['HYBRID_COMMIT_SHA']))")"
DB_PATH="${OUTPUT_DIR}/${SLUG}.db"

echo "[index_build_repo_commit] slug=$SLUG db=$DB_PATH" >&2

run_cli() {
  (cd "$HYBRID_ROOT" && "$PYTHON" -m hybrid_platform.cli --config "$CONFIG_PATH" "$@")
}

# 统一阶段标记，便于上层一键脚本与日志采集定位失败步骤
run_stage() {
  local stage="$1"
  shift
  echo ">>> PIPELINE_STAGE_START: $stage" >&2
  local rc=0
  run_cli "$@" || rc=$?
  if [[ "$rc" -ne 0 ]]; then
    echo ">>> PIPELINE_STAGE_FAILED: $stage exit_code=$rc" >&2
    exit "$rc"
  fi
  echo ">>> PIPELINE_STAGE_OK: $stage" >&2
}

if [[ -n "$PREBUILT_SCIP" ]]; then
  if [[ ! -f "$PREBUILT_SCIP" ]]; then
    echo "[index_build_repo_commit] ERROR: --prebuilt-scip 不是可读文件: $PREBUILT_SCIP" >&2
    exit 2
  fi
  if command -v realpath >/dev/null 2>&1; then
    PREBUILT_SCIP="$(realpath "$PREBUILT_SCIP")"
  else
    PREBUILT_SCIP="$(cd "$(dirname "$PREBUILT_SCIP")" && pwd)/$(basename "$PREBUILT_SCIP")"
  fi
  echo "[index_build_repo_commit] using prebuilt SCIP: $PREBUILT_SCIP" >&2
  run_stage ingest ingest --repo "$REPO_NAME" --commit "$COMMIT_SHA" --db "$DB_PATH" \
    --input "$PREBUILT_SCIP" --source-root "$REPO_ROOT"
  if [[ "${SKIP_CODE_GRAPH:-0}" != "1" ]]; then
    run_stage build-code-graph build-code-graph --db "$DB_PATH" --repo "$REPO_NAME" --commit "$COMMIT_SHA"
  fi
  if [[ "${SKIP_CHUNK:-0}" != "1" ]]; then
    run_stage chunk chunk --db "$DB_PATH" --repo "$REPO_NAME" --commit "$COMMIT_SHA"
  fi
  if [[ "${SKIP_EMBED:-0}" != "1" ]]; then
    run_stage embed embed --db "$DB_PATH"
  fi
else
  BUILD_ARGS=(build-java-index --repo-root "$REPO_ROOT" --repo "$REPO_NAME" --commit "$COMMIT_SHA" --db "$DB_PATH")
  if [[ -n "${BUILD_TOOL:-}" ]]; then
    BUILD_ARGS+=(--build-tool "$BUILD_TOOL")
  fi
  # argparse 会把「以 - 开头」的 Maven/Gradle 参数误当成 CLI 选项，必须在前面加 --
  if [[ ${#EXTRA_JAVA[@]} -gt 0 ]]; then
    BUILD_ARGS+=(--)
    BUILD_ARGS+=("${EXTRA_JAVA[@]}")
  fi
  run_stage build-java-index "${BUILD_ARGS[@]}"
fi

echo "[index_build_repo_commit] done slug=$SLUG" >&2
echo "HYBRID_DB=$DB_PATH"
echo "HYBRID_MCP_PATH=/mcp/${SLUG}"
echo "MCP_URL=http://\${HYBRID_MCP_HOST:-0.0.0.0}:\${HYBRID_MCP_PORT:-8765}/mcp/${SLUG}"
