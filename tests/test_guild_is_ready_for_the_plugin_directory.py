"""Guild as a plugin in the OpenAI directory and a Claude connector: every tool
passes the directory's review, the shared parts are nal-mcp-sdk's, and the
package checks clean. Pure, no network: runs in CI."""

import asyncio
import json
import tomllib
from pathlib import Path

import pytest

from guild_mcp._nal import plugin, vendor
from guild_mcp._nal.tools import audit_tools
from guild_mcp.server import mcp

ROOT = Path(__file__).resolve().parents[1]
COPY = ROOT / "guild_mcp" / "_nal"
SDK = ROOT.parents[1] / "nal-common-team" / "shared" / "nal-mcp-sdk" / "nal_mcp_sdk" / "hosted"
VERSION = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["tool"]["poetry"][
    "version"
]


def _all_tools():
    # Every registered tool, whatever GUILD_MCP_PROFILE hides: the full
    # profile is what a hosted deployment may list.
    return [t.to_mcp_tool() for t in asyncio.run(mcp.get_tools()).values()]


def test_every_tool_passes_the_scan():
    tools = _all_tools()
    assert len(tools) == 59
    assert [str(p) for p in audit_tools(tools, prefix="guild_")] == []


def test_a_guild_reaches_beyond_the_account_and_says_so():
    # Posts are public and written by others: open world, every tool.
    assert all(t.annotations.openWorldHint is True for t in _all_tools())


def test_the_scope_follows_the_tag_and_the_hint_follows_the_effect():
    for t in asyncio.run(mcp.get_tools()).values():
        m = t.to_mcp_tool()
        (scheme,) = m.meta["securitySchemes"]
        want = "social-graph:write" if "write" in t.tags else "social-graph:read"
        assert scheme == {"type": "oauth2", "scopes": [want]}, t.name
        if "write" in t.tags:
            assert m.annotations.readOnlyHint is False, t.name
    # The one read that is not read-only: it writes nothing but is billed and
    # calls an LLM, so a host must not auto-approve it (see server.py).
    not_read_only = {
        n
        for n, t in asyncio.run(mcp.get_tools()).items()
        if "read" in t.tags and not t.to_mcp_tool().annotations.readOnlyHint
    }
    assert not_read_only == {"guild_saved_feed_preview"}


def test_the_copy_is_unedited():
    assert vendor.drift(COPY) == []


@pytest.mark.skipif(not SDK.is_dir(), reason="nal-mcp-sdk is not checked out beside this repo")
def test_the_copy_is_the_current_sdk():
    assert vendor.drift(COPY, sdk=SDK) == []


def test_the_package_checks_clean():
    assert plugin.check(ROOT / "openai-plugin", version=VERSION, skills=ROOT / "skills") == []


def test_both_plugins_say_the_same_version():
    claude = json.loads((ROOT / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))
    assert claude["version"] == VERSION
