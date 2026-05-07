#!/usr/bin/env bash
# Run embed then eval-retrieval-compare for one model under var/spring-eval/models/<NAME>/.
# Prerequisites:
#   - Shared SQLite DB already built (build-java-index).
#   - models/<NAME>/config.json exists (embedding.* + vector.lancedb.uri for this model).
#
# Usage:
#   SPRING_EVAL_ROOT=/abs/path/var/spring-eval \
#     scripts/run_one_model_eval.sh <NAME> <SHARED_DB> <DATASET_JSONL> <REPO> <COMMIT> [--top-k K ...]
#
# Example:
#   cd hybrid_platform
#   SPRING_EVAL_ROOT="$PWD/var/spring-eval" \
#     scripts/run_one_model_eval.sh qwen3-emb-8b \
#       "$PWD/var/spring-eval/index/spring-6ec2455e.db" \
#       "$PWD/../JAVA test/spring_framework_eval_v1_reviewed.verify.jsonl" \
#       spring-projects/spring-framework \
#       6ec2455e2491650fbeb7efaf78615a72700995ad \
#       --top-k 5 --top-k 10

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HP_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "${HP_ROOT}"

PYTHON="${PYTHON:-${HP_ROOT}/myenv/bin/python}"

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  sed -n '1,25p' "$0" | tail -n +2
  exit 0
fi

if [[ $# -lt 5 ]]; then
  echo "usage: SPRING_EVAL_ROOT=<dir> $0 <NAME> <SHARED_DB> <DATASET_JSONL> <REPO> <COMMIT> [--top-k K ...]" >&2
  exit 2
fi

NAME="$1"
SHARED_DB="$2"
DATASET_JSONL="$3"
REPO="$4"
COMMIT="$5"
shift 5

SPRING_EVAL_ROOT="${SPRING_EVAL_ROOT:-${HP_ROOT}/var/spring-eval}"
CFG="${SPRING_EVAL_ROOT}/models/${NAME}/config.json"
OUT="${SPRING_EVAL_ROOT}/models/${NAME}/report.json"

if [[ ! -f "${CFG}" ]]; then
  echo "error: missing config: ${CFG}" >&2
  exit 2
fi

mkdir -p "$(dirname "${OUT}")"

echo "[embed] db=${SHARED_DB} config=${CFG}"
"${PYTHON}" -m hybrid_platform.cli --config "${CFG}" embed --db "${SHARED_DB}"

echo "[eval-retrieval-compare] output=${OUT}"
"${PYTHON}" -m hybrid_platform.cli --config "${CFG}" eval-retrieval-compare \
  --db "${SHARED_DB}" \
  --repo "${REPO}" \
  --commit "${COMMIT}" \
  --dataset "${DATASET_JSONL}" \
  "$@" \
  --output "${OUT}"

echo "done: ${OUT}"
