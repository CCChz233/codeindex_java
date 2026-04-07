#!/usr/bin/env bash
# 公网/边缘后的 MCP URL 快速探测：Bearer 401 与带令牌时非 502。
# 用法：
#   ./scripts/verify_mcp_edge_proxy.sh 'https://codeindex.example.com' 'your-bearer-token'
#   ./scripts/verify_mcp_edge_proxy.sh 'http://127.0.0.1:8765'   # 未启用 Bearer 时仅检查可达
# 文档：docs/deploy_public_mcp_edge_proxy.md

set -euo pipefail

BASE="${1:?usage: $0 <base_url_no_trailing_slash> [bearer_token]}"
TOKEN="${2:-}"

# 规范化 path：默认探测 /mcp
MCP_PATH="/mcp"
URL="${BASE%/}${MCP_PATH}"

if [[ -n "$TOKEN" ]]; then
  code_no_auth=$(curl -sS -o /dev/null -w "%{http_code}" -X POST "$URL" \
    -H "Content-Type: application/json" \
    -d '{}' || true)
  if [[ "$code_no_auth" == "401" ]]; then
    echo "OK: POST without Authorization returned 401 (Bearer required)."
  else
    echo "WARN: expected HTTP 401 without Authorization on POST, got: $code_no_auth"
    echo "      (If Bearer is disabled on server, this warning is expected.)"
  fi

  code_auth=$(curl -sS -o /dev/null -w "%{http_code}" -X POST "$URL" \
    -H "Content-Type: application/json" \
    -H "Authorization: Bearer ${TOKEN}" \
    -d '{}' || true)
  if [[ "$code_auth" == "502" ]] || [[ "$code_auth" == "503" ]]; then
    echo "FAIL: upstream/gateway error HTTP $code_auth (check proxy timeouts and backend)."
    exit 1
  fi
  if [[ "$code_auth" == "401" ]]; then
    echo "FAIL: still 401 with Bearer (token mismatch or proxy stripped Authorization)."
    exit 1
  fi
  echo "OK: POST with Bearer got HTTP $code_auth (not 401/502/503; app may return 4xx for empty JSON-RPC — acceptable)."
else
  code=$(curl -sS -o /dev/null -w "%{http_code}" -X GET "$URL" || true)
  echo "GET $URL -> HTTP $code (no token check; enable Bearer and pass token as 2nd arg for full check)."
fi
