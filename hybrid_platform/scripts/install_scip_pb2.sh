#!/usr/bin/env bash
# 从 sourcegraph/scip 生成 Python scip_pb2，供 binary .scip ingest 使用。
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
VENV_PY="${ROOT}/myenv/bin/python"
if [ ! -x "$VENV_PY" ]; then
  echo "缺少 $VENV_PY，请先创建虚拟环境" >&2
  exit 1
fi
"$VENV_PY" -m pip install -q protobuf
G="$(mktemp -d)"
trap 'rm -rf "$G"' EXIT
curl -sL -o "$G/scip.proto" "https://raw.githubusercontent.com/sourcegraph/scip/main/scip.proto"
protoc --python_out="$G" --proto_path="$G" "$G/scip.proto"
SP="$("$VENV_PY" -c "import site; print(site.getsitepackages()[0])")"
cp "$G/scip_pb2.py" "$SP/scip_pb2.py"
mkdir -p "$SP/scip"
cp "$G/scip_pb2.py" "$SP/scip/scip_pb2.py"
touch "$SP/scip/__init__.py"
"$VENV_PY" -c "from scip import scip_pb2; print('scip_pb2 OK')"
