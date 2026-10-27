"""The parts every hosted NAL MCP server shares, and that ChatGPT, Claude and
Codex plugins need.

- ``oauth``: sign-in through nal-users for remote MCP clients (FastMCP
  OAuthProxy), and the caller's token to forward to the backend.
- ``tools``: the review rules for tool metadata (three explicit hints, a
  title, a prefix), checked by ``audit_tools`` in each server's tests.
- ``apps``: MCP Apps widgets (``text/html;profile=mcp-app``) with their CSP.
- ``spotlight``: the fence around text other people wrote.
- ``plugin``: builds and checks a plugin package (plugin.json, mcp.json,
  skills/) for the OpenAI plugin directory, which Codex shares.

Servers that ship publicly (guild-mcp) cannot depend on this private repo, so
this package is vendored byte for byte by ``python -m nal_mcp_sdk.hosted.vendor``;
each consumer has a test that fails when its copy drifts.

Needs ``fastmcp>=2.14,<3`` in the consumer; nal-mcp-sdk itself does not.
"""
