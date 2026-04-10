#!/usr/bin/env bash
# 在 Docker 内对挂载的源码树运行 scip-java，将 index.scip 写到宿主机路径。
#
# 用法：
#   ./docker_scip_build.sh /abs/path/to/repo [/abs/path/to/index.scip] [-- scip-java 额外参数...]
#
# 环境变量：
#   SCIP_BUILD_IMAGE 镜像名（默认 hybrid-scip-build:local）
#   JAVA_TOOL_OPTIONS / MAVEN_OPTS  传入容器（可选）
#
set -euo pipefail

IMAGE="${SCIP_BUILD_IMAGE:-hybrid-scip-build:local}"

if [[ $# -lt 1 ]]; then
  echo "usage: $0 REPO_ROOT [OUTPUT_SCIP] [-- scip-java args...]" >&2
  exit 2
fi

REPO_HOST="$1"
shift
OUT_HOST=""
EXTRA=()
if [[ $# -ge 1 && "$1" != "--" ]]; then
  OUT_HOST="$1"
  shift
fi
if [[ "${1:-}" == "--" ]]; then
  shift
  EXTRA=("$@")
fi

REPO_HOST="$(cd "$REPO_HOST" && pwd)"
if [[ -z "$OUT_HOST" ]]; then
  OUT_HOST="${REPO_HOST}/index.scip"
else
  OUT_HOST="$(cd "$(dirname "$OUT_HOST")" && pwd)/$(basename "$OUT_HOST")"
fi
OUT_DIR="$(dirname "$OUT_HOST")"
OUT_BASE="$(basename "$OUT_HOST")"

echo "[docker_scip_build] image=$IMAGE repo=$REPO_HOST out=$OUT_HOST" >&2

docker run --rm \
  -v "${REPO_HOST}:/work" \
  -v "${OUT_DIR}:/out" \
  -w /work \
  -e "JAVA_TOOL_OPTIONS=${JAVA_TOOL_OPTIONS:-}" \
  -e "MAVEN_OPTS=${MAVEN_OPTS:-}" \
  "$IMAGE" \
  scip-java index --output "/out/${OUT_BASE}" "${EXTRA[@]}"

echo "[docker_scip_build] wrote $OUT_HOST" >&2
