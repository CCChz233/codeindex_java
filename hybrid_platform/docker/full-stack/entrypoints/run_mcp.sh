#!/usr/bin/env bash
set -euo pipefail

PYTHON="${HYBRID_PYTHON:-/usr/local/bin/python}"

if [[ -z "${HYBRID_DB:-}" ]]; then
  echo "HYBRID_DB is required." >&2
  exit 2
fi
if [[ -z "${HYBRID_CONFIG:-}" ]]; then
  echo "HYBRID_CONFIG is required." >&2
  exit 2
fi
if [[ ! -f "$HYBRID_DB" ]]; then
  echo "HYBRID_DB does not exist: $HYBRID_DB" >&2
  exit 1
fi
if [[ ! -f "$HYBRID_CONFIG" ]]; then
  echo "HYBRID_CONFIG does not exist: $HYBRID_CONFIG" >&2
  exit 1
fi

exec "$PYTHON" -m hybrid_platform.cli mcp-streamable --db "$HYBRID_DB" --config "$HYBRID_CONFIG" "$@"
