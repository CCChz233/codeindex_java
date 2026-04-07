"""Codeindex MCP（stdio）：只读工具含 ``semantic_query`` / ``find_symbol`` / ``symbol_graph``；多跳图探索未通过 MCP 暴露。索引管理（purge 等）请用 HTTP ``serve`` 的 ``/admin/*``，勿暴露给 Agent。

``semantic_query`` / ``symbol_graph`` 在 MCP 上仅暴露最小参数集（``query`` 或 ``op``+``symbol_id``）；``mode``/``top_k``/代码片段等由服务端固定，完整调参请用 HTTP ``serve`` 的 ``/query`` 与 ``/query/structured``。

环境变量：``HYBRID_DB``（必填，已有索引的 .db）；``HYBRID_CONFIG``（可选，默认 ``hybrid_platform/config/default_config.json``）。

运行::

    export HYBRID_DB=/path/to/index.db   # 注意等号两侧不要空格
    export HYBRID_CONFIG=/path/to/config.json
    python -m hybrid_platform.mcp_server

进程启动后会**阻塞且无终端输出**（stdout 留给 MCP 协议）；在交互终端下会向 **stderr** 打一行说明。应由 Cursor 等客户端拉起，而不是当普通 CLI 等它「跑完」。

**云端远程部署**请使用 Streamable HTTP 入口：``python -m hybrid_platform.mcp_streamable_server``（见该模块文档）。

Cursor MCP 配置示例（command 指向本仓库 venv 的 python）::

    {
      "mcpServers": {
        "codeindex": {
          "command": "/path/to/hybrid_platform/myenv/bin/python",
          "args": ["-m", "hybrid_platform.mcp_server"],
          "env": {"HYBRID_DB": "/path/to/index.db"}
        }
      }
    }
"""

from __future__ import annotations

import asyncio
import os
import sys

from mcp.server.fastmcp import FastMCP

from .mcp_server_instructions import MCP_SERVER_INSTRUCTIONS
from .mcp_tools_registry import register_codeindex_tools

mcp = FastMCP("hybrid-codeindex", instructions=MCP_SERVER_INSTRUCTIONS)
register_codeindex_tools(mcp)


def main() -> None:
    # stdout 专用于 MCP JSON-RPC；说明性输出只能走 stderr，且避免在 Cursor 子进程里刷屏。
    if sys.stderr.isatty():
        db = (os.environ.get("HYBRID_DB") or "").strip()
        print(
            "[hybrid-codeindex MCP] stdio transport waiting for client (no stdout output is normal). "
            f"HYBRID_DB={db!r}. Connect from an MCP client (e.g. Cursor); do not expect an interactive shell prompt.",
            file=sys.stderr,
            flush=True,
        )
    asyncio.run(mcp.run_stdio_async())


if __name__ == "__main__":
    try:
        main()
    except BrokenPipeError:
        sys.exit(0)
