#!/usr/bin/env bash
# Create var/spring-eval/models/<NAME>/config.json for an OpenAI-compatible
# embedding endpoint. The script probes the embedding dimension before writing.
#
# Usage:
#   scripts/create_embedding_model_config.sh <NAME> <MODEL> [API_BASE]
#
# Example:
#   scripts/create_embedding_model_config.sh \
#     bge-code-v1 \
#     /data_nvme0/models/embedding/bge-code-v1 \
#     http://118.196.65.175:8000/v1
#
# Environment overrides:
#   SPRING_EVAL_ROOT  default: <hybrid_platform>/var/spring-eval
#   PYTHON            default: <hybrid_platform>/myenv/bin/python
#   ENDPOINT          default: /embeddings
#   TIMEOUT_S         default: 120
#   BATCH_SIZE        default: 8
#   MAX_WORKERS       default: 2
#   FORCE=1           overwrite existing config.json

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HP_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "${HP_ROOT}"

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  sed -n '1,24p' "$0" | tail -n +2
  exit 0
fi

if [[ $# -lt 2 || $# -gt 3 ]]; then
  echo "usage: $0 <NAME> <MODEL> [API_BASE]" >&2
  exit 2
fi

NAME="$1"
MODEL="$2"
API_BASE="${3:-${API_BASE:-http://118.196.65.175:8000/v1}}"
ENDPOINT="${ENDPOINT:-/embeddings}"
TIMEOUT_S="${TIMEOUT_S:-120}"
BATCH_SIZE="${BATCH_SIZE:-8}"
MAX_WORKERS="${MAX_WORKERS:-2}"

PYTHON="${PYTHON:-${HP_ROOT}/myenv/bin/python}"
SPRING_EVAL_ROOT="${SPRING_EVAL_ROOT:-${HP_ROOT}/var/spring-eval}"
MODEL_DIR="${SPRING_EVAL_ROOT}/models/${NAME}"
LANCEDB_DIR="${MODEL_DIR}/lancedb"
CONFIG_PATH="${MODEL_DIR}/config.json"

if [[ -f "${CONFIG_PATH}" && "${FORCE:-0}" != "1" ]]; then
  echo "error: config already exists: ${CONFIG_PATH}" >&2
  echo "set FORCE=1 to overwrite" >&2
  exit 2
fi

mkdir -p "${LANCEDB_DIR}"

echo "[probe] model=${MODEL}"
echo "[probe] api_base=${API_BASE}${ENDPOINT}"

"${PYTHON}" - "${NAME}" "${MODEL}" "${API_BASE}" "${ENDPOINT}" "${TIMEOUT_S}" \
  "${BATCH_SIZE}" "${MAX_WORKERS}" "${LANCEDB_DIR}" "${CONFIG_PATH}" <<'PY'
import json
import sys
from pathlib import Path

from hybrid_platform.embedding import HttpEmbeddingClient

name, model, api_base, endpoint, timeout_s, batch_size, max_workers, lancedb_dir, config_path = sys.argv[1:]
timeout_s_int = int(timeout_s)

client = HttpEmbeddingClient(
    model=model,
    api_base=api_base,
    endpoint=endpoint,
    timeout_s=timeout_s_int,
)
vectors = client.embed_batch(["hello", "world"])
if len(vectors) != 2 or len(vectors[0]) != len(vectors[1]):
    raise SystemExit("embedding probe returned inconsistent batch vectors")
dim = len(vectors[0])

config = {
    "java_index": {
        "source_backend": "tree-sitter-java",
    },
    "chunk": {
        "target_tokens": 1024,
        "overlap_tokens": 48,
        "token_counter": "auto",
        "strategy": "ast",
        "java_treesitter_fallback": True,
        "java_container_policy": "leaf_preferred",
        "fallback_to_definition_span": True,
        "ast_min_lines": 5,
        "include_leading_doc_comment": True,
        "include_call_graph_context": True,
        "call_context_max_each": 8,
        "leading_doc_max_lookback_lines": 120,
        "function_level_only": True,
        "ast_parent_min_lines": 8,
        "ast_parent_min_tokens": 100,
        "sibling_merge_enabled": True,
        "sibling_merge_small_max_tokens": 100,
        "sibling_merge_target_tokens": 260,
        "sibling_merge_max_gap_lines": 3,
    },
    "embedding": {
        "version": name,
        "provider": "http",
        "model": model,
        "dim": dim,
        "api_base": api_base,
        "endpoint": endpoint,
        "api_key": "",
        "timeout_s": timeout_s_int,
        "batch_size": int(batch_size),
        "max_workers": int(max_workers),
        "max_retries": 3,
        "retry_backoff_s": 1.0,
        "stream_fetch_limit": 512,
        "stream_commit_every_batches": 256,
        "stream_write_buffer_chunks": 512,
        "provider_max_concurrency": int(max_workers),
        "online_max_concurrency": int(max_workers),
        "online_query_max_retries": 3,
        "online_query_cache_size": 512,
        "online_query_cache_ttl_s": 300.0,
        "fail_open_on_query": True,
    },
    "vector": {
        "backend": "lancedb",
        "write_mode": "lancedb_only",
        "lancedb": {
            "uri": str(Path(lancedb_dir).resolve()),
            "table": "chunk_vectors",
            "metric": "cosine",
        },
    },
}

path = Path(config_path)
path.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
print(f"[ok] dim={dim}")
print(f"[ok] wrote {path}")
print(f"[ok] lancedb={Path(lancedb_dir).resolve()}")
PY
