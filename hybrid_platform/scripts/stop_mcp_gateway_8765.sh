#!/usr/bin/env bash
# 停止 start_mcp_gateway_8765.sh 拉起的 MCP 子进程与本 runtime 下的 nginx。
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HYBRID_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
PYTHON="${HYBRID_PYTHON:-${HYBRID_ROOT}/myenv/bin/python}"
cd "$HYBRID_ROOT"

EXTRA=()
[[ -n "${MCP_GATEWAY_RUNTIME:-}" ]] && EXTRA+=(--runtime-dir "$MCP_GATEWAY_RUNTIME")

exec "$PYTHON" -m hybrid_platform.mcp_gateway_local stop "${EXTRA[@]}" "$@"
