#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HYBRID_ROOT="${HYBRID_ROOT:-$(cd "$SCRIPT_DIR/../../.." && pwd)}"
PYTHON="${HYBRID_PYTHON:-/usr/local/bin/python}"
DEFAULT_OUTPUT_DIR="/data/hybrid_indices"
DEFAULT_METADATA_FILE="/data/index_metadata.json"
DEFAULT_WORKSPACE_ROOT="/workspace/repos"

usage() {
  cat >&2 <<'EOF'
index_repo_commit.sh — 全容器标准索引入口

必填：
  --repo-name NAME
  --commit SHA
  --config PATH

二选一：
  --git-url URL      标准复现模式：容器内 clone 到 /workspace/repos/<slug>
  --repo-root PATH   开发模式：使用已挂载的源码目录

可选：
  --output-dir PATH
  --build-tool TOOL
  --prebuilt-scip PATH

额外的 scip-java / Maven / Gradle 参数请写在单独的 -- 之后。
EOF
}

abs_path_existing() {
  local raw="${1:?}"
  if [[ -d "$raw" ]]; then
    (cd "$raw" && pwd)
    return 0
  fi
  local dir
  dir="$(cd "$(dirname "$raw")" && pwd)"
  printf '%s/%s\n' "$dir" "$(basename "$raw")"
}

normalize_path() {
  local raw="${1:?}"
  if [[ "$raw" = /* ]]; then
    printf '%s\n' "$raw"
  else
    printf '%s/%s\n' "$PWD" "$raw"
  fi
}

REPO_NAME=""
COMMIT=""
CONFIG_PATH=""
GIT_URL=""
REPO_ROOT=""
OUTPUT_DIR="$DEFAULT_OUTPUT_DIR"
BUILD_TOOL=""
PREBUILT_SCIP=""
EXTRA_ARGS=()

while [[ $# -gt 0 ]]; do
  case "$1" in
    --repo-name|--repo)
      REPO_NAME="${2:?}"
      shift 2
      ;;
    --commit)
      COMMIT="${2:?}"
      shift 2
      ;;
    --config)
      CONFIG_PATH="${2:?}"
      shift 2
      ;;
    --git-url)
      GIT_URL="${2:?}"
      shift 2
      ;;
    --repo-root)
      REPO_ROOT="${2:?}"
      shift 2
      ;;
    --output-dir)
      OUTPUT_DIR="${2:?}"
      shift 2
      ;;
    --build-tool)
      BUILD_TOOL="${2:?}"
      shift 2
      ;;
    --prebuilt-scip)
      PREBUILT_SCIP="${2:?}"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    --)
      shift
      EXTRA_ARGS=("$@")
      break
      ;;
    *)
      echo "Unknown option: $1" >&2
      exit 2
      ;;
  esac
done

if [[ -z "$REPO_NAME" || -z "$COMMIT" || -z "$CONFIG_PATH" ]]; then
  echo "Required: --repo-name, --commit, --config" >&2
  usage
  exit 2
fi

if [[ -n "$GIT_URL" && -n "$REPO_ROOT" ]]; then
  echo "Use exactly one of --git-url or --repo-root." >&2
  exit 2
fi
if [[ -z "$GIT_URL" && -z "$REPO_ROOT" ]]; then
  echo "One of --git-url or --repo-root is required." >&2
  exit 2
fi

CONFIG_PATH="$(abs_path_existing "$CONFIG_PATH")"
if [[ ! -f "$CONFIG_PATH" ]]; then
  echo "Config file not found: $CONFIG_PATH" >&2
  exit 1
fi

COMMIT="$(printf '%s' "$COMMIT" | tr '[:upper:]' '[:lower:]')"
OUTPUT_DIR="$(normalize_path "$OUTPUT_DIR")"
METADATA_FILE="$(normalize_path "${INDEX_METADATA_FILE:-$DEFAULT_METADATA_FILE}")"

mkdir -p "$OUTPUT_DIR" "$(dirname "$METADATA_FILE")" "$DEFAULT_WORKSPACE_ROOT"

export HYBRID_PYTHON="$PYTHON"
export INDEX_METADATA_FILE="$METADATA_FILE"

SLUG="$("$PYTHON" -c "from hybrid_platform.index_slug import repo_commit_slug; print(repo_commit_slug(${REPO_NAME@Q}, ${COMMIT@Q}))")"
if [[ -n "$GIT_URL" ]]; then
  DEST="${DEFAULT_WORKSPACE_ROOT}/${SLUG}"
else
  DEST="$(abs_path_existing "$REPO_ROOT")"
fi

echo "[full-stack indexer] repo=${REPO_NAME} commit=${COMMIT} slug=${SLUG}" >&2
echo "[full-stack indexer] output_dir=${OUTPUT_DIR} metadata=${METADATA_FILE}" >&2
if [[ -n "$GIT_URL" ]]; then
  echo "[full-stack indexer] mode=clone-in-container dest=${DEST}" >&2
else
  echo "[full-stack indexer] mode=mounted-repo repo_root=${DEST} (development path; not the standard reproducible mode)" >&2
fi

if [[ -n "$PREBUILT_SCIP" ]]; then
  PREBUILT_SCIP="$(abs_path_existing "$PREBUILT_SCIP")"
  if [[ ! -f "$PREBUILT_SCIP" ]]; then
    echo "Prebuilt SCIP not found: $PREBUILT_SCIP" >&2
    exit 1
  fi
  if [[ -n "$GIT_URL" ]]; then
    bash "$HYBRID_ROOT/scripts/clone_repo_at_commit.sh" \
      --git-url "$GIT_URL" \
      --commit "$COMMIT" \
      --dest "$DEST"
  elif [[ ! -d "$DEST" ]]; then
    echo "Repo root not found: $DEST" >&2
    exit 1
  fi

  BUILD_CMD=(
    bash "$HYBRID_ROOT/scripts/index_build_repo_commit.sh"
    --config "$CONFIG_PATH"
    --repo-name "$REPO_NAME"
    --commit "$COMMIT"
    --repo-root "$DEST"
    --output-dir "$OUTPUT_DIR"
    --prebuilt-scip "$PREBUILT_SCIP"
  )
  if [[ -n "$BUILD_TOOL" ]]; then
    BUILD_CMD+=(--build-tool "$BUILD_TOOL")
  fi
  if [[ ${#EXTRA_ARGS[@]} -gt 0 ]]; then
    BUILD_CMD+=(--)
    BUILD_CMD+=("${EXTRA_ARGS[@]}")
  fi
  "${BUILD_CMD[@]}"

  "$PYTHON" -m hybrid_platform.index_metadata upsert \
    --repo "$REPO_NAME" \
    --commit "$COMMIT" \
    --config "$CONFIG_PATH" \
    --output-dir "$OUTPUT_DIR" \
    --metadata-file "$METADATA_FILE"
else
  INDEX_CMD=(
    bash "$HYBRID_ROOT/scripts/repo_commit_to_index.sh"
    --config "$CONFIG_PATH"
    --repo-name "$REPO_NAME"
    --commit "$COMMIT"
    --dest "$DEST"
    --output-dir "$OUTPUT_DIR"
  )
  if [[ -n "$GIT_URL" ]]; then
    INDEX_CMD+=(--git-url "$GIT_URL")
  else
    if [[ ! -d "$DEST" ]]; then
      echo "Repo root not found: $DEST" >&2
      exit 1
    fi
    INDEX_CMD+=(--skip-clone)
  fi
  if [[ -n "$BUILD_TOOL" ]]; then
    INDEX_CMD+=(--build-tool "$BUILD_TOOL")
  fi
  if [[ ${#EXTRA_ARGS[@]} -gt 0 ]]; then
    INDEX_CMD+=(--)
    INDEX_CMD+=("${EXTRA_ARGS[@]}")
  fi
  "${INDEX_CMD[@]}"
fi

echo "[full-stack indexer] done slug=${SLUG}" >&2
