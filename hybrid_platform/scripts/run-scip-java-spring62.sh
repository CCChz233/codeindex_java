#!/usr/bin/env bash
# Spring Framework 6.2.x 使用 Kotlin 1.9.x，需 scip-java ≤0.10.x（semanticdb-kotlinc 0.4.0）。
# 0.11+ 的 kotlinc 插件与 1.9 编译器不兼容（如 MESSAGE_COLLECTOR_KEY）。
set -euo pipefail
BIN="${SCIP_JAVA_SPRING62:-/data1/qadong/bin/scip-java-0.10.4}"
export JAVA_HOME="${JAVA_HOME:-/usr/lib/jvm/java-17-openjdk-amd64}"
TMP_ROOT="${SCIP_JAVA_TMPDIR:-/data1/qadong/tmp}"
mkdir -p "$TMP_ROOT"
export TMPDIR="$TMP_ROOT"
export JAVA_TOOL_OPTIONS="${JAVA_TOOL_OPTIONS:+${JAVA_TOOL_OPTIONS} }-Djava.io.tmpdir=${TMP_ROOT}"
exec "$BIN" "$@"
