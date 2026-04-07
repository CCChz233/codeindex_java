"""MCP 进程内共享的 Codeindex 运行时（stdio 与 Streamable HTTP 入口共用）。"""

from __future__ import annotations

import os
from pathlib import Path

from .agent_mcp_handlers import CodeindexMcpRuntime

_DEFAULT_CONFIG = str(Path(__file__).resolve().parents[1] / "config" / "default_config.json")

_runtime: CodeindexMcpRuntime | None = None


def default_config_path() -> str:
    return (os.environ.get("HYBRID_CONFIG") or "").strip() or _DEFAULT_CONFIG


def get_mcp_runtime() -> CodeindexMcpRuntime | None:
    """懒加载；HYBRID_DB 须为存在的 .db 文件。"""
    global _runtime
    if _runtime is not None:
        return _runtime
    db = (os.environ.get("HYBRID_DB") or "").strip()
    if not db:
        return None
    p = Path(db)
    if not p.is_file():
        return None
    _runtime = CodeindexMcpRuntime(str(p.resolve()), default_config_path())
    return _runtime


def reset_mcp_runtime_for_tests() -> None:
    global _runtime
    if _runtime is not None:
        try:
            _runtime.close()
        except Exception:
            pass
    _runtime = None
