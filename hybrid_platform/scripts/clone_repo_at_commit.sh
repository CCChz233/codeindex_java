#!/usr/bin/env bash
# 克隆（或更新）仓库并检出指定 commit，供 index_build_repo_commit.sh 的 --repo-root 使用。
#
# 必填：--git-url URL  --commit SHA  --dest PATH
# 可选：
#   --recurse-submodules  含子模块（与 --shallow 同时开时，子模块仍尽量浅拉）
#   --shallow             浅 fetch 指定 commit（体积小；若远端不允许按 SHA 浅取或历史不够会失败，请去掉后重试）
#
# 默认 **不是** 浅 clone：普通 git clone 再 checkout，任意可达 commit 最稳。
#
# JDK / 平台：本脚本只负责 Git；Java 版本请在构建时用 --java-home 或 JAVA_HOME（见 docs/java_index_repo_setup.md）。
#
set -euo pipefail

usage() {
  cat >&2 <<'EOF'
clone_repo_at_commit.sh — 检出指定 commit 的源码树

必填：
  --git-url URL
  --commit SHA
  --dest PATH

可选：
  --recurse-submodules
  --shallow            仅浅取该 commit（见脚本头注释）

示例（ls1intum/Artemis @ 7364749…）：
  ./scripts/clone_repo_at_commit.sh \
    --git-url https://github.com/ls1intum/Artemis.git \
    --commit 7364749a8de08befd5f96e9dfecf6d13e241944a \
    --dest /data/worktrees/Artemis-7364749 \
    --recurse-submodules
EOF
}

GIT_URL=""
COMMIT=""
DEST=""
RECURSE=0
SHALLOW=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --git-url)
      GIT_URL="${2:?}"
      shift 2
      ;;
    --commit)
      COMMIT="${2:?}"
      shift 2
      ;;
    --dest)
      DEST="${2:?}"
      shift 2
      ;;
    --recurse-submodules)
      RECURSE=1
      shift
      ;;
    --shallow)
      SHALLOW=1
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown option: $1" >&2
      exit 2
      ;;
  esac
done

if [[ -z "$GIT_URL" || -z "$COMMIT" || -z "$DEST" ]]; then
  echo "Required: --git-url, --commit, --dest (--help for usage)" >&2
  exit 2
fi

COMMIT="$(printf '%s' "$COMMIT" | tr '[:upper:]' '[:lower:]')"

CLONE_OPTS=()
CO_OPTS=()
if [[ "$RECURSE" -eq 1 ]]; then
  CLONE_OPTS+=(--recurse-submodules)
  CO_OPTS+=(--recurse-submodules)
fi

if [[ -e "$DEST" && ! -d "$DEST" ]]; then
  echo "Destination exists and is not a directory: $DEST" >&2
  exit 1
fi

if [[ -d "$DEST/.git" ]] || [[ -f "$DEST/.git" ]]; then
  echo "[clone_repo_at_commit] existing repo, fetch + checkout: $DEST" >&2
  if [[ "$SHALLOW" -eq 1 ]]; then
    git -C "$DEST" fetch --depth 1 origin "$COMMIT" || {
      echo "[clone_repo_at_commit] shallow fetch failed; try without --shallow or increase depth manually" >&2
      exit 1
    }
  else
    git -C "$DEST" fetch origin
  fi
  git -C "$DEST" checkout "${CO_OPTS[@]}" "$COMMIT"
else
  if [[ -d "$DEST" ]] && [[ -n "$(ls -A "$DEST" 2>/dev/null || true)" ]]; then
    echo "Destination exists, is not a git repo, and is not empty: $DEST" >&2
    exit 1
  fi
  mkdir -p "$(dirname "$DEST")"
  if [[ "$SHALLOW" -eq 1 ]]; then
    echo "[clone_repo_at_commit] shallow init + fetch commit -> $DEST" >&2
    git init "$DEST"
    git -C "$DEST" remote add origin "$GIT_URL"
    if ! git -C "$DEST" fetch --depth 1 origin "$COMMIT"; then
      echo "[clone_repo_at_commit] shallow fetch failed (server may disallow SHA fetch or need full clone). Retry without --shallow." >&2
      exit 1
    fi
    git -C "$DEST" checkout "${CO_OPTS[@]}" FETCH_HEAD
  else
    echo "[clone_repo_at_commit] full git clone -> $DEST" >&2
    git clone "${CLONE_OPTS[@]}" "$GIT_URL" "$DEST"
    git -C "$DEST" checkout "${CO_OPTS[@]}" "$COMMIT"
  fi
fi

echo "[clone_repo_at_commit] OK repo-root=$DEST HEAD=$(git -C "$DEST" rev-parse HEAD)" >&2
