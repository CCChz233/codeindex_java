"""ASGI wrapper: optional Bearer auth in front of Streamable HTTP."""

from __future__ import annotations

import asyncio
import json

from hybrid_platform.mcp_streamable_asgi import compose_optional_bearer_auth


class _CaptureSend:
    def __init__(self) -> None:
        self.messages: list[dict] = []

    async def __call__(self, message: dict) -> None:
        self.messages.append(message)


def test_bearer_disabled_passes_through() -> None:
    async def _body() -> None:
        async def inner(scope: dict, receive, send) -> None:
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"ok"})

        app = compose_optional_bearer_auth(inner, None)
        cap = _CaptureSend()

        async def receive():
            return {"type": "http.disconnect"}

        await app(
            {
                "type": "http",
                "asgi": {"version": "3.0"},
                "http_version": "1.1",
                "method": "GET",
                "path": "/mcp",
                "raw_path": b"/mcp",
                "headers": [],
            },
            receive,
            cap,
        )
        assert cap.messages[0]["status"] == 200

    asyncio.run(_body())


def test_bearer_required_rejects_missing() -> None:
    async def _body() -> None:
        async def inner(scope: dict, receive, send) -> None:
            pytest.fail("inner should not run")

        app = compose_optional_bearer_auth(inner, "secret-token")
        cap = _CaptureSend()

        async def receive():
            return {"type": "http.disconnect"}

        await app(
            {
                "type": "http",
                "asgi": {"version": "3.0"},
                "http_version": "1.1",
                "method": "POST",
                "path": "/mcp",
                "raw_path": b"/mcp",
                "headers": [],
            },
            receive,
            cap,
        )
        assert cap.messages[0]["status"] == 401
        body = cap.messages[1]["body"]
        err = json.loads(body.decode())
        assert err["error"] == "unauthorized"

    asyncio.run(_body())


def test_bearer_required_accepts_valid() -> None:
    async def _body() -> None:
        async def inner(scope: dict, receive, send) -> None:
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"ok"})

        app = compose_optional_bearer_auth(inner, "good")
        cap = _CaptureSend()

        async def receive():
            return {"type": "http.disconnect"}

        await app(
            {
                "type": "http",
                "asgi": {"version": "3.0"},
                "http_version": "1.1",
                "method": "POST",
                "path": "/mcp",
                "raw_path": b"/mcp",
                "headers": [
                    (b"authorization", b"Bearer good"),
                ],
            },
            receive,
            cap,
        )
        assert cap.messages[0]["status"] == 200

    asyncio.run(_body())
