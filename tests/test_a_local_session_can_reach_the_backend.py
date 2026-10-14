"""A stdio session must be able to authenticate from inside a tool call.

`_get_client()` runs on the server's event loop, and `guild_mcp.auth.authenticate()`
drives its own `asyncio.run()` to validate the token. Called directly, every
backend tool failed with "asyncio.run() cannot be called from a running event
loop" -- only `guild_taxonomy`, which reads bundled data, ever answered. The
unit suites never noticed because none of them reach `_get_client()` with a
token to validate; the failure only showed against a live backend.
"""

from __future__ import annotations

import asyncio

import pytest

import guild_mcp.auth
import guild_mcp.server as server


@pytest.fixture
def stdio_session(monkeypatch):
    async def _validate(_cfg, token):
        return "user-87" if token == "valid-jwt" else None

    monkeypatch.setenv("NAL_API_TOKEN", "valid-jwt")
    monkeypatch.setattr(guild_mcp.auth, "_validate", _validate)
    monkeypatch.setattr(guild_mcp.auth, "_load_cached_token", lambda _cfg: None)
    monkeypatch.setattr(server, "_HTTP_MODE", False)
    monkeypatch.setattr(server, "_client", None)
    monkeypatch.setattr(server, "_init_lock", asyncio.Lock())
    yield
    server._client = None


def test_a_tool_call_authenticates_on_a_running_loop(stdio_session):
    async def a_tool_call():
        return await server._get_client()

    client = asyncio.run(a_tool_call())

    assert client._user_id == "user-87"
    assert client._token == "valid-jwt"


def test_the_control_authenticate_on_the_loop_is_what_broke(stdio_session):
    # Negative control: the direct call the fix replaced must still fail here,
    # or the test above proves nothing about the loop.
    async def the_old_call():
        return guild_mcp.auth.authenticate(scopes=["social-graph:read"], interactive=False)

    with pytest.raises(RuntimeError, match="running event loop"):
        asyncio.run(the_old_call())
