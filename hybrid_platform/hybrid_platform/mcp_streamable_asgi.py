"""Streamable HTTP 外层 ASGI：可选 Bearer 鉴权（与 MCP 工具读写分层独立）。"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable

Send = Callable[..., Awaitable[None]]
Receive = Callable[[], Awaitable[dict]]


def _header_dict(scope: dict) -> dict[str, str]:
    return {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers") or []}


def _json_401(www_authenticate: str) -> Callable[[Receive, Send], Awaitable[None]]:
    body = json.dumps(
        {"error": "unauthorized", "message": "Missing or invalid Authorization Bearer token"},
        ensure_ascii=False,
    ).encode("utf-8")

    async def _send401(receive: Receive, send: Send) -> None:
        await send(
            {
                "type": "http.response.start",
                "status": 401,
                "headers": [
                    [b"content-type", b"application/json; charset=utf-8"],
                    [b"www-authenticate", www_authenticate.encode("ascii")],
                    [b"content-length", str(len(body)).encode("ascii")],
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})

    return _send401


def compose_optional_bearer_auth(inner: Callable, bearer_token: str | None) -> Callable:
    """在 inner ASGI 外检查 ``Authorization: Bearer``；``bearer_token`` 为 None 时不校验。

    将 ``lifespan`` 与 ``http`` 原样交给 inner，以便 FastMCP Streamable 会话与启动钩子正常工作。
    """
    if not bearer_token:
        return inner

    expected_hdr = f"Bearer {bearer_token}"
    www = f'Bearer realm="hybrid-codeindex-mcp", error="invalid_token"'

    async def app(scope: dict, receive: Receive, send: Send) -> None:
        if scope["type"] == "lifespan":
            await inner(scope, receive, send)
            return
        if scope["type"] == "http":
            headers = _header_dict(scope)
            auth = headers.get("authorization", "")
            if auth != expected_hdr:
                await _json_401(www)(receive, send)
                return
        await inner(scope, receive, send)

    return app
