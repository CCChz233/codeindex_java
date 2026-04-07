#!/usr/bin/env bash
# 读 index_metadata.json，在 127.0.0.1:28065… 起多个 MCP，再用 Nginx 监听 8765 按 /mcp/<slug> 转发。
# 依赖：系统已安装 nginx（PATH 可执行）；Python 用 hybrid_platform/myenv。
#
# 环境变量：
#   INDEX_METADATA_FILE   默认 hybrid_platform/var/index_metadata.json
#   MCP_GATEWAY_RUNTIME   默认 hybrid_platform/var/mcp_gateway/runtime
#
# 参数传给 python -m hybrid_platform.mcp_gateway_local start，例如：
#   ./scripts/start_mcp_gateway_8765.sh --listen 8765 --backend-base 28065
#   ./scripts/start_mcp_gateway_8765.sh --no-stop-first   # 不先杀旧进程（易冲突）
#
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HYBRID_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
PYTHON="${HYBRID_PYTHON:-${HYBRID_ROOT}/myenv/bin/python}"
cd "$HYBRID_ROOT"

EXTRA=()
[[ -n "${INDEX_METADATA_FILE:-}" ]] && EXTRA+=(--metadata-file "$INDEX_METADATA_FILE")
[[ -n "${MCP_GATEWAY_RUNTIME:-}" ]] && EXTRA+=(--runtime-dir "$MCP_GATEWAY_RUNTIME")

exec "$PYTHON" -m hybrid_platform.mcp_gateway_local start --listen 8765 "${EXTRA[@]}" "$@"
