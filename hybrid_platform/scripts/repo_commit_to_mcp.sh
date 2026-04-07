#!/usr/bin/env bash
# 兼容入口：等同于 repo_commit_to_index.sh（已不再在本脚本内启动 MCP）。
# 构建完成后请执行：scripts/start_mcp_gateway_8765.sh
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec "$SCRIPT_DIR/repo_commit_to_index.sh" "$@"
