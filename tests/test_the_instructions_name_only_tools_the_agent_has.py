"""Every tool the server's own instructions tell the agent to call is exposed.

The `instructions` block makes deduplication mandatory -- "ALWAYS
guild_search_by_anchor" -- while that tool was tagged `extended`, so under the
default `core` profile the agent was ordered to call a tool it did not have.
"""

from __future__ import annotations

import asyncio
import re

import guild_mcp.server as server


def _tools() -> dict:
    return asyncio.run(server.mcp.get_tools())


def test_every_tool_the_instructions_name_is_in_the_default_profile():
    named = set(re.findall(r"\b(guild_\w+)\(", server.mcp.instructions))
    tools = _tools()

    assert named, "the instructions stopped naming tools; this gate checks nothing"
    assert named <= set(tools), f"instructions name tools that do not exist: {named - set(tools)}"
    not_core = {n for n in named if "core" not in tools[n].tags}
    assert not not_core, f"instructions require tools hidden by the core profile: {not_core}"


def test_the_control_an_extended_tool_is_really_hidden_by_core():
    # Negative control: if `core` stopped filtering, the gate above would pass
    # for any tag at all.
    assert "core" not in _tools()["guild_related"].tags
    assert server._INCLUDE_TAGS in ({"core"}, None)


def _documented_link_types() -> set[str]:
    doc = _tools()["guild_create_link"].description
    return set(re.findall(r"^\s*- (\w+): ", doc, re.M))


def test_every_link_type_the_docs_suggest_is_one_the_tool_lists():
    # The backend accepts `reference`; `references`, `supports` and
    # `summarizes` were suggested in two docstrings and each one is a 422.
    listed = _documented_link_types()
    assert {"extends", "reference", "contradicts"} <= listed

    tools = _tools()
    text = server.mcp.instructions + "".join(t.description or "" for t in tools.values())
    suggested = set(re.findall(r"link_type='(\w+)'", text))
    suggested |= set(re.findall(r"→ '(\w+)'", text))
    assert suggested, "no suggestions found; this gate checks nothing"
    assert suggested <= listed, (
        f"docs suggest link types the tool does not list: {suggested - listed}"
    )
