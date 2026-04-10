#!/usr/bin/env bash
# Java 测评索引准备统一入口：derive / build / validate
#
# 示例：
#   ./scripts/java_eval_index_prep.sh derive \
#     --manifest "/data1/qadong/codeindex_java/JAVA test/test_java_agent_manifest_size_ge_100000.jsonl"
#
#   ./scripts/java_eval_index_prep.sh build \
#     --manifest "/data1/qadong/codeindex_java/JAVA test/test_java_agent_manifest_size_ge_100000.jsonl" \
#     --config ./config/java_eval_deterministic_config.json \
#     --sample-id netty__netty-15575

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HYBRID_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
PYTHON="${HYBRID_PYTHON:-${HYBRID_ROOT}/myenv/bin/python}"

cd "$HYBRID_ROOT"
exec "$PYTHON" -m hybrid_platform.java_eval_prep "$@"
