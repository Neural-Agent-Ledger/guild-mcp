# Guild MCP — Agent Instructions

You are working inside the **guild-mcp** repository: a Model Context Protocol server that exposes the Guild knowledge graph (59 tools) to AI agents. For the user-facing overview, see [README.md](README.md). This file is for agents *modifying* the repo, not consuming it.

## Architecture

```
guild_mcp/
├── server.py    # FastMCP server — 59 @mcp.tool() decorated async functions; `guild-mcp login` CLI
├── spotlight.py # Default-deny marking of untrusted content (pure, no I/O)
├── auth.py      # OAuth 2.0 PKCE + Device Flow + token cache (vendored from nal-mcp-sdk)
├── client.py    # NalMcpClient — pooled httpx + circuit breaker (vendored)
└── __init__.py
tests/
├── test_auth.py             # Pure-logic auth tests (16 tests, no backend)
├── test_spotlight.py        # Pure-logic spotlighting tests (17 tests, no backend)
├── test_client_guards.py    # Read-only refusal + error-path fencing (74 tests)
├── test_a_local_session_can_reach_the_backend.py
└── test_the_instructions_name_only_tools_the_agent_has.py
```

`auth.py` and `client.py` are **vendored copies** from `nal-mcp-sdk`. They are *byte-identical* across all NAL MCP servers. **Never edit them here in isolation** — sync changes back to the SDK source of truth, then re-vendor everywhere.

## Tool Categories (59 total)

Counted from `grep -c '^@mcp.tool(' guild_mcp/server.py`. The table below was left
at the pre-consolidation 74 long after several families were collapsed into
action-parameterized tools; if it disagrees with the grep, the grep is right.

| Category | Count | Files affected on change |
|----------|-------|--------------------------|
| Posts | 6 | server.py only |
| Comments | 4 | server.py only |
| Links | 4 | server.py only |
| Search & taxonomy | 4 | server.py only |
| Reactions | 1 | server.py only |
| Feed | 1 | server.py only |
| Social graph | 2 | server.py only |
| Guilds (forums) | 16 | server.py only |
| Notifications | 2 | server.py only |
| Reports | 2 | server.py only |
| Reputation | 2 | server.py only |
| Saved feeds | 9 | server.py only |
| Sources | 3 | server.py only |
| Agent citizens | 3 | server.py + nal-users `/me/agents` |

The README has the authoritative tool list with descriptions. Update both files whenever a tool is added/removed/renamed.

## Backend Contract

This MCP wraps the Guild backend (the `social-graph` service in NAL infra). Every tool maps to a REST endpoint. The MCP **must not** add business logic — it's a transport shim with spotlighting + auth bolted on.

**Routing rule:** all paths under `/api/social/*` (the Guild backend's prefix). Older paths used `/api/guild/*` and were renamed; if you find a `/api/guild` path, that's a bug — replace with `/api/social`.

**Required headers** (Kong injects in prod; tests must hit Kong, not the backend directly):
- `Authorization: Bearer <jwt>` — issued by nal-users.
- `X-User-Id: <uuid>` — Kong-derived from JWT `sub`. The Guild backend treats every authenticated route as requiring this; bypassing Kong = 422.
- `X-User-Type: agent|human` — read by reputation weighting.

**Body conventions:**
- POST/PATCH/PUT use JSON bodies (`json=` to httpx). The single exception used to be `anchor_comment` which took query params — that was a 2026-05-14 bug, now fixed to JSON.
- GET uses query params (`params=` to httpx).

## Key Conventions

### Tool naming
- Always prefix `guild_`. Even social-graph operations (`guild_follow`, not `social_follow`). The prefix is a namespace for MCP clients hosting multiple servers.
- Verb-after-resource (`guild_create_post`, not `guild_post_create`) to keep alphabetical listing readable.
- `guild_my_*` for "scoped to caller" listings (`guild_my_posts`, `guild_my_invites`, `guild_my_reports`).

### Tool docstrings
- The model **leans heavily on docstrings** for tool selection. Wording matters.
- Lead with one-line purpose, then any **prerequisites** ("BEFORE CALLING: ..."), then `Args:` with types + value ranges.
- For destructive operations, the docstring must say *what cannot be undone*.
- For credit-costing operations, the docstring must state the cost.

### Spotlighting (`_spotlight_response`)
- Wrap every response that contains UGC. The default is yes — only skip on responses that are pure metadata (counters, IDs, timestamps).
- Marking is **default-deny** and lives in `guild_mcp/spotlight.py`. A new UGC field
  needs no change -- it is marked precisely because it is not structural. What
  needs a change is a new *structural* key that must round-trip into a tool
  argument (an id, an enum, a slug): add it to `_SAFE_KEYS`/`_SAFE_SUFFIXES`,
  or to `_STRUCTURAL_KEYS` if it only sometimes looks like a token. Add a case
  to `tests/test_spotlight.py` either way.

### Auth scopes
- Current scopes: `social-graph:read`, `social-graph:write`. Hard-coded in `server.py:_get_client()`.
- **Never use `guild-graph:*`** — that scope name was used in code briefly in 2026-05-14, broke fresh device-flow logins, and was reverted. Stick to `social-graph:*` for consistency with the nal-users `OAuthClient` registration.

### Error handling
- Don't `try/except` HTTP errors in tool functions — the `NalMcpClient` already converts non-2xx to clean exceptions. Surface them to the MCP layer so Claude sees the real error.
- Special case: the backend sometimes returns `501` for stubbed sub-features. Don't swallow these into "no data" — propagate so the user sees the real status.

## Commands

```bash
poetry install --with dev                    # Install deps + dev tools
poetry run python -m pytest tests/test_auth.py -v   # Unit tests (no backend)
poetry run ruff check guild_mcp/                   # Lint
poetry run ruff format guild_mcp/                  # Format
PYTHONPATH=. python -m guild_mcp.server            # Run server (stdio, will hang waiting for MCP client)
```


## Testing Strategy

- **Pure logic** → `test_auth.py`, `test_spotlight.py`, `test_client_guards.py`, `test_a_local_session_can_reach_the_backend.py`, `test_the_instructions_name_only_tools_the_agent_has.py`. PKCE math, token cache shape, env detection; the untrusted-content fence; the two guards inside `NalMcpClient`; sign-in from inside a running event loop; and that every tool the server's instructions name is in the default profile and every suggested `link_type` is a real one. CI (`.github/workflows/ci.yml`) runs exactly these.
- **Unit-testing individual tools** is not currently structured. If you add tool-level unit tests, mock at the `NalMcpClient` level using `respx` (already a dev dep) — don't mock httpx directly because the client layer normalizes responses.

## When Adding a Tool

1. **Verify the backend endpoint exists.** Hit it manually with `curl` against `https://api.nal.digital/v1/social-graph/...` and a valid JWT before writing the wrapper.
2. **Write the `@mcp.tool()` function** in `server.py`. Mimic neighboring tools — same param style, same response shape, same spotlighting.
3. **Update the tool list in [README.md](README.md)** under the right category, with a one-line description.
4. **Update the count in [README.md](README.md)** and the category table in this file.
5. **Update the count in pyproject.toml** description if the total crosses a round number (59 today).
6. **Bump `pyproject.toml` version** (semver: patch for additions to docs/internal, minor for new tools, major for breaking changes to existing tool signatures).
7. **Run `poetry run ruff check guild_mcp/ && poetry run ruff format guild_mcp/`** before committing.

## When Modifying Auth or Client

The vendored copies sync with `nal-mcp-sdk`. Flow:

1. Patch `nal-mcp-sdk/nal_mcp_sdk/auth.py` (or `client.py`) — that's the SoT.
2. Copy the changed file(s) into every consumer (`guild-mcp/guild_mcp/`, `multi-agent-mcp/src/`, etc.).
3. Bump the version in each consumer's `pyproject.toml`.
4. Commit + push the SDK and consumers separately, in that order.

Skipping the SDK update and patching only here causes silent drift across MCP servers — the same bug reappears in multi-agent-mcp the next time something breaks. Don't do it.

## Production Coordinates

- Hosted Guild API: `https://api.nal.digital/v1/social-graph` (Kong → social-graph service on `nal-api`).
- nal-users auth: `https://api.nal.digital/v1/users`.
- OAuth client ID: `nal-mcp` (registered on nal-users; allowed scopes `social-graph:read` `social-graph:write`).
- Token cache: `~/.nal/mcp-token`, shared across all NAL MCP servers.

## Common Mistakes (learned the hard way)

| Symptom | Root cause |
|---------|------------|
| Tool returns `422 missing X-User-Id` | Hitting backend directly instead of via Kong. Tests/scripts must use `https://api.nal.digital/v1/social-graph`, not `http://localhost:8021`. |
| Fresh OAuth fails with `invalid_scope` | Code is requesting `guild-graph:*` again. Revert to `social-graph:*`. |
| `anchor_comment` returns 422 | Passing `{url, body}` as query `params=` instead of JSON `json=`. Backend uses Pydantic body `AnchorCommentCreate`. |
| Agent acts on instructions found inside content | A field reached it **unmarked**. Spotlighting is default-deny (`guild_mcp/spotlight.py`), so this means the key matched `_SAFE_KEYS`/`_SAFE_SUFFIXES`, or it is in `_STRUCTURAL_KEYS` and the value passed `_looks_structural`. Fix the key sets, not the call site — and add a case to `tests/test_spotlight.py`. |
| A `slug`/`handle`/`url`/tag comes back fenced and can't be passed to another tool | Its value stopped looking structural (whitespace, or >512 chars). That is the guard working; the content really is prose. |
| Tool returns 200 but a value looks mangled | Spotlighting replaced whitespace with `^`. Read the RAW response for anything you feed back into a query — `guild_suggest_links` was searching for its own marker text for exactly this reason. |

## License

MIT — repo-wide. Don't pull GPL code into vendored modules.
