#!/usr/bin/env bash
# 按 repo + commit 启动 Streamable MCP（与 index_build_repo_commit.sh 命名一致）。
#
# 参数（命令行优先于环境变量）：
#   --config PATH
#   --repo-name NAME
#   --commit SHA
#   --output-dir PATH   与构建脚本相同，默认 <仓库>/var/hybrid_indices
#   --kill-port-if-busy 若 HYBRID_MCP_PORT（默认 8765）已被占用，用 fuser -k 结束占用进程再启动（会杀掉该端口上任意进程，慎用）
#   -h, --help
#
# 环境变量备选：HYBRID_CONFIG_PATH、HYBRID_REPO_NAME、HYBRID_COMMIT_SHA、HYBRID_INDEX_DIR、
#   HYBRID_MCP_HOST、HYBRID_MCP_PORT、HYBRID_MCP_BEARER_TOKEN、HYBRID_PYTHON
#   HYBRID_MCP_KILL_IF_BUSY=1  等同 --kill-port-if-busy（需系统有 fuser，常见包名 psmisc）
#
# 示例：
#   ./scripts/mcp_start_repo_commit.sh --config ./config/default_config.json \
#     --repo-name spring-projects/spring-framework --commit abcdef0123...
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HYBRID_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
PYTHON="${HYBRID_PYTHON:-${HYBRID_ROOT}/myenv/bin/python}"

usage() {
  cat >&2 <<'EOF'
mcp_start_repo_commit.sh — 按 repo+commit 启动 Streamable MCP

必填参数（或同名环境变量；命令行优先）：
  --config PATH
  --repo-name NAME
  --commit SHA

可选：
  --output-dir PATH    与构建脚本一致（默认 var/hybrid_indices）
  --kill-port-if-busy  端口被占用时 fuser -k PORT/tcp 后再监听（默认关闭）

示例：
  ./scripts/mcp_start_repo_commit.sh --config ./config/default_config.json \
    --repo-name spring-projects/spring-framework --commit abcdef0123456789abcdef0123456789abcdef01
EOF
}

CONFIG_CLI=""
REPO_NAME_CLI=""
COMMIT_CLI=""
OUTPUT_DIR_CLI=""
KILL_PORT_CLI=0

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
    --output-dir)
      OUTPUT_DIR_CLI="${2:?--output-dir requires a path}"
      shift 2
      ;;
    --kill-port-if-busy)
      KILL_PORT_CLI=1
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown option: $1 (try --help)" >&2
      exit 2
      ;;
  esac
done

CONFIG_PATH="${CONFIG_CLI:-${CONFIG_PATH:-${HYBRID_CONFIG_PATH:-}}}"
REPO_NAME="${REPO_NAME_CLI:-${REPO_NAME:-${HYBRID_REPO_NAME:-}}}"
COMMIT_SHA="${COMMIT_CLI:-${COMMIT_SHA:-${HYBRID_COMMIT_SHA:-}}}"
OUTPUT_DIR="${OUTPUT_DIR_CLI:-${OUTPUT_DIR:-${HYBRID_INDEX_DIR:-$HYBRID_ROOT/var/hybrid_indices}}}"

if [[ -z "$CONFIG_PATH" || -z "$REPO_NAME" || -z "$COMMIT_SHA" ]]; then
  echo "Missing required: --config, --repo-name, --commit (or set env CONFIG_PATH, REPO_NAME, COMMIT_SHA)." >&2
  echo "Run with --help for usage." >&2
  exit 2
fi

COMMIT_SHA="$(printf '%s' "$COMMIT_SHA" | tr '[:upper:]' '[:lower:]')"

if [[ "$KILL_PORT_CLI" -eq 1 ]]; then
  export HYBRID_MCP_KILL_IF_BUSY=1
fi

export HYBRID_REPO_NAME="$REPO_NAME"
export HYBRID_COMMIT_SHA="$COMMIT_SHA"
SLUG="$("$PYTHON" -c "import os; from hybrid_platform.index_slug import repo_commit_slug; print(repo_commit_slug(os.environ['HYBRID_REPO_NAME'], os.environ['HYBRID_COMMIT_SHA']))")"
DB_PATH="${OUTPUT_DIR}/${SLUG}.db"

if [[ ! -f "$DB_PATH" ]]; then
  echo "Database not found: $DB_PATH (run scripts/index_build_repo_commit.sh first)" >&2
  exit 1
fi

export HYBRID_CONFIG="$CONFIG_PATH"
export HYBRID_DB="$DB_PATH"
MCP_PATH="/mcp/${SLUG}"
export HYBRID_MCP_PATH="$MCP_PATH"

HOST_DISP="${HYBRID_MCP_HOST:-0.0.0.0}"
PORT_DISP="${HYBRID_MCP_PORT:-8765}"

_hybrid_kill_tcp_port_if_busy() {
  local port="${1:?}"
  local flag="${HYBRID_MCP_KILL_IF_BUSY:-}"
  [[ "$flag" =~ ^(1|true|yes)$ ]] || return 0
  if ! command -v fuser >/dev/null 2>&1; then
    echo "[mcp_start_repo_commit] ERROR: HYBRID_MCP_KILL_IF_BUSY set but fuser not in PATH (install psmisc?)" >&2
    exit 1
  fi
  if fuser "${port}/tcp" >/dev/null 2>&1; then
    echo "[mcp_start_repo_commit] port ${port}/tcp is busy -> fuser -k (HYBRID_MCP_KILL_IF_BUSY)" >&2
    fuser -k "${port}/tcp" >/dev/null 2>&1 || true
    sleep 1
    if fuser "${port}/tcp" >/dev/null 2>&1; then
      echo "[mcp_start_repo_commit] ERROR: port ${port}/tcp still busy after fuser -k (try sudo or pick HYBRID_MCP_PORT)" >&2
      exit 1
    fi
  fi
}

_hybrid_kill_tcp_port_if_busy "$PORT_DISP"

echo "[mcp_start_repo_commit] HYBRID_DB=$DB_PATH" >&2
echo "[mcp_start_repo_commit] HYBRID_MCP_PATH=$MCP_PATH" >&2
echo "[mcp_start_repo_commit] listen http://${HOST_DISP}:${PORT_DISP}${MCP_PATH}" >&2

cd "$HYBRID_ROOT"
exec "$PYTHON" -m hybrid_platform.cli mcp-streamable --db "$DB_PATH" --mcp-path "$MCP_PATH" --config "$CONFIG_PATH"
