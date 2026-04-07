#!/usr/bin/env bash
# scip-java：Maven 上无 0.11.5，使用 Coursier 打好的 0.11.2 单文件（与 0.11.5 需求最接近）。
# 生成方式：cs bootstrap com.sourcegraph:scip-java_2.13:0.11.2 -o ... --standalone -M com.sourcegraph.scip_java.ScipJava
# 可用 SCIP_JAVA_011 覆盖二进制路径。
set -euo pipefail
BIN="${SCIP_JAVA_011:-/data1/qadong/bin/scip-java-0.11.2}"
export JAVA_HOME="${JAVA_HOME:-/usr/lib/jvm/java-17-openjdk-amd64}"
exec "$BIN" "$@"
