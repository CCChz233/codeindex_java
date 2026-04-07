"""Codeindex MCP — **Streamable HTTP** 传输（云端部署，无 stdio）。

使用 FastMCP 内置 ``streamable-http`` + Uvicorn。工具集与 ``mcp_server``（stdio）相同，经
``mcp_tools_registry`` 注册，业务仍走 ``CodeindexMcpRuntime``。

环境变量
--------

- ``HYBRID_DB``（必填）：SQLite 索引路径
- ``HYBRID_CONFIG``（可选）：JSON 配置
- ``HYBRID_MCP_HOST``（默认 ``0.0.0.0``）
- ``HYBRID_MCP_PORT``（默认 ``8765``）
- ``HYBRID_MCP_PATH``（默认 ``/mcp``）：Streamable HTTP 挂载路径（与 FastMCP 一致）
- ``HYBRID_MCP_STATELESS``（默认 ``1``）：``1``/``true`` 时每请求新建传输，利于多副本无会话亲和；``0`` 关闭
- ``HYBRID_MCP_BEARER_TOKEN``（可选）：若设置，则所有 HTTP 请求必须带
  ``Authorization: Bearer <token>``（**传输层鉴权**，与工具是否只读无关）

**读写分层**

- 本入口仅暴露 **只读** MCP 工具（``readOnlyHint=true``）；不通过 MCP 做 purge/写库。
- 管理写操作请使用 ``cli serve`` 的 ``/admin/*`` + ``HYBRID_ADMIN_TOKEN``（另一进程、另一密钥）。

运行::

    export HYBRID_DB=/path/to/index.db
    export HYBRID_MCP_BEARER_TOKEN=your-secret   # 生产建议必设
    python -m hybrid_platform.mcp_streamable_server

客户端（概念）需在 MCP 连接配置中使用 **Streamable HTTP** 指向
``http(s)://<host>:<port><HYBRID_MCP_PATH>``，并按 SDK 要求携带 Bearer（若启用）。
"""

from __future__ import annotations

import os
import sys

from mcp.server.fastmcp import FastMCP

from .mcp_server_instructions import MCP_STREAMABLE_INSTRUCTIONS
from .mcp_streamable_asgi import compose_optional_bearer_auth
from .mcp_tools_registry import register_codeindex_tools


def _stateless_from_env() -> bool:
    v = (os.environ.get("HYBRID_MCP_STATELESS") or "1").strip().lower()
    return v not in ("0", "false", "no", "off")


def _build_mcp() -> FastMCP:
    host = (os.environ.get("HYBRID_MCP_HOST") or "0.0.0.0").strip()
    port = int((os.environ.get("HYBRID_MCP_PORT") or "8765").strip())
    path = (os.environ.get("HYBRID_MCP_PATH") or "/mcp").strip()
    if not path.startswith("/"):
        path = "/" + path

    mcp = FastMCP(
        "hybrid-codeindex-remote",
        instructions=MCP_STREAMABLE_INSTRUCTIONS,
        host=host,
        port=port,
        streamable_http_path=path,
        stateless_http=_stateless_from_env(),
    )
    register_codeindex_tools(mcp)
    return mcp


def build_streamable_app():
    """返回可用于 Uvicorn / Gunicorn 的 ASGI 应用（已套可选 Bearer）。"""
    mcp = _build_mcp()
    inner = mcp.streamable_http_app()
    token = (os.environ.get("HYBRID_MCP_BEARER_TOKEN") or "").strip() or None
    return compose_optional_bearer_auth(inner, token)


def main() -> None:
    import asyncio

    import uvicorn

    mcp = _build_mcp()
    inner = mcp.streamable_http_app()
    token = (os.environ.get("HYBRID_MCP_BEARER_TOKEN") or "").strip() or None
    app = compose_optional_bearer_auth(inner, token)

    if sys.stderr.isatty():
        print(
            "[hybrid-codeindex MCP Streamable HTTP] "
            f"listening http://{mcp.settings.host}:{mcp.settings.port}{mcp.settings.streamable_http_path} "
            f"bearer_required={bool(token)} HYBRID_DB={(os.environ.get('HYBRID_DB') or '')!r}",
            file=sys.stderr,
            flush=True,
        )

    async def _run() -> None:
        config = uvicorn.Config(
            app,
            host=mcp.settings.host,
            port=mcp.settings.port,
            log_level=os.environ.get("HYBRID_MCP_LOG_LEVEL", "info").lower(),
        )
        server = uvicorn.Server(config)
        await server.serve()

    asyncio.run(_run())


if __name__ == "__main__":
    main()
