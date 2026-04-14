#!/usr/bin/env bash
# 在 Docker 内对挂载的源码树运行 scip-java，将 index.scip 写到宿主机路径。
#
# 用法：
#   ./docker_scip_build.sh /abs/path/to/repo [/abs/path/to/index.scip] [-- scip-java 额外参数...]
#
# 环境变量：
#   SCIP_BUILD_IMAGE 镜像名（默认 hybrid-scip-build:local）
#   JAVA_TOOL_OPTIONS / MAVEN_OPTS  传入容器（可选）
#   GRADLE_USER_HOME / MAVEN_REPO_LOCAL  传入容器（可选；默认落到 /data1/qadong 下）
#   DOCKER_APT_PACKAGES  空格分隔的 apt 包名（容器启动时动态安装；可选）
#   DOCKER_PRE_SCRIPT    在 scip-java 前执行的 shell 片段（可选）
#   DOCKER_EXTRA_ENV     逗号分隔的 KEY=VALUE 对，会作为 docker run -e 传入容器（可选）
#   SCIP_NO_ROBUST       设为 1 则不注入默认 skip flags（可选；调试用）
#
set -euo pipefail

# ── 鲁棒性参数：跳过所有与 Java 编译无关的 Maven 插件 ──
# scip-java 只需要 compile 阶段产生 semanticdb，verify 阶段的检查/打包/前端全部是多余的。
# 通过 MAVEN_OPTS 注入（JVM 系统属性），Maven 插件会自动读取这些属性。
# 对 Gradle 项目无影响（Gradle 不读 MAVEN_OPTS）。
ROBUST_MAVEN_FLAGS=""
if [[ "${SCIP_NO_ROBUST:-0}" != "1" ]]; then
  ROBUST_MAVEN_FLAGS="\
 -Denforcer.skip=true\
 -Dmaven.javadoc.skip=true\
 -Dmaven.test.skip=true\
 -Dcheckstyle.skip=true\
 -Dpmd.skip=true\
 -Dspotbugs.skip=true\
 -Dfrontend.skip=true\
 -Dskip.npm=true\
 -Dskip.yarn=true\
 -Dmaven.site.skip=true\
 -Dgpg.skip=true\
 -Drat.skip=true\
 -Djacoco.skip=true\
 -Danimal.sniffer.skip=true\
 -Dmaven.source.skip=true\
 -Dformatter.skip=true\
 -Dimpsort.skip=true\
 -Dmdep.analyze.skip=true\
 -Dexec.skip=true\
 -Drevapi.skip=true"
fi

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
MAVEN_REPO_LOCAL="${MAVEN_REPO_LOCAL:-/data1/qadong/.m2/repository}"
GRADLE_USER_HOME="${GRADLE_USER_HOME:-/data1/qadong/.gradle}"
TMP_ROOT="${TMPDIR:-/data1/qadong/tmp}"

mkdir -p "$OUT_DIR"
mkdir -p "$MAVEN_REPO_LOCAL" "$GRADLE_USER_HOME" "$TMP_ROOT"

# 合并鲁棒性参数到 MAVEN_OPTS（Maven 作为子进程会读取此变量）
_BASE_MAVEN_OPTS="${MAVEN_OPTS:-"-Dmaven.repo.local=${MAVEN_REPO_LOCAL}"}"
MAVEN_OPTS="${_BASE_MAVEN_OPTS}${ROBUST_MAVEN_FLAGS:+ ${ROBUST_MAVEN_FLAGS}}"

APT_PKGS="${DOCKER_APT_PACKAGES:-}"
PRE_SCRIPT="${DOCKER_PRE_SCRIPT:-}"
NEED_WRAPPER=0
[[ -n "$APT_PKGS" || -n "$PRE_SCRIPT" ]] && NEED_WRAPPER=1

echo "[docker_scip_build] image=$IMAGE repo=$REPO_HOST out=$OUT_HOST" >&2
if [[ -n "$APT_PKGS" ]]; then
  echo "[docker_scip_build] docker_packages=$APT_PKGS" >&2
fi
if [[ -n "$ROBUST_MAVEN_FLAGS" ]]; then
  echo "[docker_scip_build] robust_flags=enabled (via MAVEN_OPTS)" >&2
fi

DOCKER_CMD=(
  docker run --rm
  -v "${REPO_HOST}:/work"
  -v "${OUT_DIR}:/out"
  -v "${MAVEN_REPO_LOCAL}:${MAVEN_REPO_LOCAL}"
  -v "${GRADLE_USER_HOME}:${GRADLE_USER_HOME}"
  -v "${TMP_ROOT}:${TMP_ROOT}"
  -w /work
  -e "JAVA_TOOL_OPTIONS=${JAVA_TOOL_OPTIONS:-"-Djava.io.tmpdir=${TMP_ROOT}"}"
  -e "MAVEN_OPTS=${MAVEN_OPTS}"
  -e "GRADLE_USER_HOME=${GRADLE_USER_HOME}"
  -e "TMPDIR=${TMP_ROOT}"
  -e "TMP=${TMP_ROOT}"
  -e "TEMP=${TMP_ROOT}"
)

if [[ -n "${DOCKER_EXTRA_ENV:-}" ]]; then
  IFS=',' read -ra _pairs <<< "$DOCKER_EXTRA_ENV"
  for _pair in "${_pairs[@]}"; do
    [[ -n "$_pair" ]] && DOCKER_CMD+=(-e "$_pair")
  done
fi

if [[ "$NEED_WRAPPER" -eq 1 ]]; then
  DOCKER_CMD+=(-e "DOCKER_APT_PACKAGES=${APT_PKGS}" -e "DOCKER_PRE_SCRIPT=${PRE_SCRIPT}")
  DOCKER_CMD+=("$IMAGE" bash -c '
set -e
if [ -n "$DOCKER_APT_PACKAGES" ]; then
  apt-get update -qq && apt-get install -y --no-install-recommends $DOCKER_APT_PACKAGES && rm -rf /var/lib/apt/lists/*
fi
if [ -n "$DOCKER_PRE_SCRIPT" ]; then
  eval "$DOCKER_PRE_SCRIPT"
fi
exec scip-java index --output "'""/out/${OUT_BASE}""'" "$@"
')
  if [[ ${#EXTRA[@]} -gt 0 ]]; then
    DOCKER_CMD+=(-- "${EXTRA[@]}")
  fi
else
  DOCKER_CMD+=("$IMAGE" scip-java index --output "/out/${OUT_BASE}")
  if [[ ${#EXTRA[@]} -gt 0 ]]; then
    DOCKER_CMD+=("${EXTRA[@]}")
  fi
fi

"${DOCKER_CMD[@]}"

echo "[docker_scip_build] wrote $OUT_HOST" >&2
