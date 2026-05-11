#!/usr/bin/env bash
# One-command Spring embedding evaluation.
#
# Typical usage:
#   scripts/run_spring_eval.sh bge-code-v1 /data_nvme0/models/embedding/bge-code-v1 --rebuild
#
# What it does:
#   1. Probe embedding dim and write models/<NAME>/config.json.
#   2. Build or reuse the shared Spring index DB.
#   3. Run eval-retrieval-compare for this model.
#   4. Regenerate aggregate summary.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HP_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "${HP_ROOT}"

usage() {
  cat <<'EOF'
usage:
  scripts/run_spring_eval.sh <NAME> <MODEL> [API_BASE] [options]

examples:
  # Code/index logic changed: rebuild shared DB, then eval bge-code-v1.
  scripts/run_spring_eval.sh bge-code-v1 /data_nvme0/models/embedding/bge-code-v1 --rebuild

  # Only rerun this model on an existing DB.
  scripts/run_spring_eval.sh bge-code-v1 /data_nvme0/models/embedding/bge-code-v1

options:
  --rebuild                  rebuild the shared SQLite DB before eval
  --force-config             overwrite models/<NAME>/config.json
  --db PATH                  shared DB path
                             default: var/spring-eval/index/spring-6ec2455e-ts-symbolctx.db
  --repo-root PATH           Spring source checkout
                             default: /data1/qadong/workspace/spring-framework
  --dataset PATH             eval JSONL
                             default: /data1/qadong/codeindex_java/JAVA test/spring_framework_eval_v1_reviewed.verify.jsonl
  --repo NAME                default: spring-projects/spring-framework
  --commit SHA               default: 6ec2455e2491650fbeb7efaf78615a72700995ad
  --api-base URL             default: http://118.196.65.175:8000/v1
  --top-k K                  repeatable; default: --top-k 5 --top-k 10
  --no-aggregate             skip aggregate summary generation
  -h, --help                 show this help

env overrides:
  SPRING_EVAL_ROOT           default: <hybrid_platform>/var/spring-eval
  PYTHON                     default: <hybrid_platform>/myenv/bin/python
  ENDPOINT                   default: /embeddings
  TIMEOUT_S                  default: 120
  BATCH_SIZE                 default: 8
  MAX_WORKERS                default: 2
EOF
}

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  usage
  exit 0
fi

if [[ $# -lt 2 ]]; then
  usage >&2
  exit 2
fi

NAME="$1"
MODEL="$2"
shift 2

API_BASE="${API_BASE:-http://118.196.65.175:8000/v1}"
if [[ $# -gt 0 && "${1:-}" != --* ]]; then
  API_BASE="$1"
  shift
fi

PYTHON="${PYTHON:-${HP_ROOT}/myenv/bin/python}"
SPRING_EVAL_ROOT="${SPRING_EVAL_ROOT:-${HP_ROOT}/var/spring-eval}"
DB="${SPRING_EVAL_ROOT}/index/spring-6ec2455e-ts-symbolctx.db"
REPO_ROOT="/data1/qadong/workspace/spring-framework"
DATASET="/data1/qadong/codeindex_java/JAVA test/spring_framework_eval_v1_reviewed.verify.jsonl"
REPO="spring-projects/spring-framework"
COMMIT="6ec2455e2491650fbeb7efaf78615a72700995ad"
REBUILD=0
FORCE_CONFIG=0
AGGREGATE=1
TOP_K_ARGS=()

while [[ $# -gt 0 ]]; do
  case "$1" in
    --rebuild)
      REBUILD=1
      shift
      ;;
    --force-config)
      FORCE_CONFIG=1
      shift
      ;;
    --db)
      DB="${2:?missing value for --db}"
      shift 2
      ;;
    --repo-root)
      REPO_ROOT="${2:?missing value for --repo-root}"
      shift 2
      ;;
    --dataset)
      DATASET="${2:?missing value for --dataset}"
      shift 2
      ;;
    --repo)
      REPO="${2:?missing value for --repo}"
      shift 2
      ;;
    --commit)
      COMMIT="${2:?missing value for --commit}"
      shift 2
      ;;
    --api-base)
      API_BASE="${2:?missing value for --api-base}"
      shift 2
      ;;
    --top-k)
      TOP_K_ARGS+=(--top-k "${2:?missing value for --top-k}")
      shift 2
      ;;
    --no-aggregate)
      AGGREGATE=0
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "error: unknown arg: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

if [[ ${#TOP_K_ARGS[@]} -eq 0 ]]; then
  TOP_K_ARGS=(--top-k 5 --top-k 10)
fi

MODEL_DIR="${SPRING_EVAL_ROOT}/models/${NAME}"
CFG="${MODEL_DIR}/config.json"
REPORT="${MODEL_DIR}/report.json"
AGG_DIR="${SPRING_EVAL_ROOT}/aggregate"
TODAY="$(date +%F)"
AGG_JSON="${AGG_DIR}/summary_${TODAY}_${NAME}.json"
AGG_MD="${AGG_DIR}/summary_${TODAY}_${NAME}.md"

mkdir -p "${SPRING_EVAL_ROOT}/index" "${MODEL_DIR}/lancedb" "${AGG_DIR}"

echo "[spring-eval] name=${NAME}"
echo "[spring-eval] model=${MODEL}"
echo "[spring-eval] api_base=${API_BASE}"
echo "[spring-eval] db=${DB}"
echo "[spring-eval] report=${REPORT}"

if [[ ! -x "${PYTHON}" ]]; then
  echo "error: python not executable: ${PYTHON}" >&2
  exit 2
fi
if [[ ! -d "${REPO_ROOT}" ]]; then
  echo "error: repo root not found: ${REPO_ROOT}" >&2
  exit 2
fi
if [[ ! -f "${DATASET}" ]]; then
  echo "error: dataset not found: ${DATASET}" >&2
  exit 2
fi

if [[ ! -f "${CFG}" || "${FORCE_CONFIG}" == "1" ]]; then
  echo "[spring-eval] create config"
  FORCE=1 SPRING_EVAL_ROOT="${SPRING_EVAL_ROOT}" PYTHON="${PYTHON}" \
    "${SCRIPT_DIR}/create_embedding_model_config.sh" "${NAME}" "${MODEL}" "${API_BASE}"
else
  echo "[spring-eval] reuse config: ${CFG}"
fi

if [[ "${REBUILD}" == "1" || ! -f "${DB}" ]]; then
  if [[ "${REBUILD}" == "1" && -f "${DB}" ]]; then
    echo "[spring-eval] rebuild requested; existing DB will be overwritten by SQLite writes: ${DB}"
  else
    echo "[spring-eval] DB missing; build required"
  fi
  "${PYTHON}" -m hybrid_platform.cli --config "${CFG}" build-java-index \
    --repo-root "${REPO_ROOT}" \
    --repo "${REPO}" \
    --commit "${COMMIT}" \
    --db "${DB}" \
    --source-backend tree-sitter-java
else
  echo "[spring-eval] reuse DB; run embed for current model"
  "${PYTHON}" -m hybrid_platform.cli --config "${CFG}" embed --db "${DB}"
fi

echo "[spring-eval] eval"
"${PYTHON}" -m hybrid_platform.cli --config "${CFG}" eval-retrieval-compare \
  --db "${DB}" \
  --repo "${REPO}" \
  --commit "${COMMIT}" \
  --dataset "${DATASET}" \
  "${TOP_K_ARGS[@]}" \
  --output "${REPORT}"

if [[ "${AGGREGATE}" == "1" ]]; then
  echo "[spring-eval] aggregate"
  "${PYTHON}" scripts/aggregate_retrieval_compare_reports.py \
    "${REPORT}" \
    --json-out "${AGG_JSON}" \
    > "${AGG_MD}"
  echo "[spring-eval] aggregate_md=${AGG_MD}"
  echo "[spring-eval] aggregate_json=${AGG_JSON}"
fi

echo "[spring-eval] done report=${REPORT}"
