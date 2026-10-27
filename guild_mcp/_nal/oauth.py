"""Sign-in for a hosted NAL MCP server, through nal-users.

nal-users is a fixed-client OAuth2/OIDC server: S256 PKCE, public clients, no
Dynamic Client Registration. ChatGPT, Claude and Codex all register
themselves, so FastMCP's OAuthProxy stands in front: it emulates DCR for the
MCP client, runs the browser flow against nal-users with one static public
client per server, and hands the MCP client its own reference token. The
nal-users access token behind it is what the backend is called with
(``forwarded_bearer``), through Kong, which derives the user from it.
"""

from __future__ import annotations

import os

from fastmcp.server.auth import OAuthProxy
from fastmcp.server.auth.providers.jwt import JWTVerifier

USERS_URL = "https://api.nal.digital/v1/users"

# Where each host sends the browser back after sign-in. ChatGPT: the fixed URI
# for servers that meet issuer identification, and the per-connector form
# otherwise (developers.openai.com/plugins/build/auth). Claude: claude.ai and
# claude.com. Codex and Claude Code: a loopback port.
CHATGPT_REDIRECTS = (
    "https://chatgpt.com/connector_platform_oauth_redirect",
    "https://chatgpt.com/connector/oauth/*",
)
CLAUDE_REDIRECTS = (
    "https://claude.ai/api/mcp/auth_callback",
    "https://claude.com/api/mcp/auth_callback",
)
LOOPBACK_REDIRECTS = ("http://localhost:*", "http://127.0.0.1:*")
ALL_HOST_REDIRECTS = CHATGPT_REDIRECTS + CLAUDE_REDIRECTS + LOOPBACK_REDIRECTS

OIDC_SCOPES = ("openid", "profile", "email")


def redirect_allowed(uri: str, redirects: tuple[str, ...] = ALL_HOST_REDIRECTS) -> bool:
    """Whether a client may be sent back to ``uri``.

    ``scheme://host:*`` is a loopback rule (any port), ``.../*`` a path prefix
    on that exact host, anything else must match exactly. Parsed, not
    globbed: ``fnmatch`` lets ``http://localhost:@evil.example/`` through.
    """
    from urllib.parse import urlsplit

    try:
        got = urlsplit(uri)
        got.port  # noqa: B018 - raises on a malformed port
    except ValueError:
        return False
    if got.username is not None or got.password is not None or got.fragment:
        return False
    for rule in redirects:
        if rule.endswith(":*"):
            want = urlsplit(rule[:-2])
            if got.scheme == want.scheme and got.hostname == want.hostname:
                return True
        elif rule.endswith("/*"):
            want = urlsplit(rule[:-1])
            if (
                got.scheme == want.scheme
                and got.netloc == want.netloc
                and got.path.startswith(want.path)
                and ".." not in got.path
            ):
                return True
        elif uri == rule:
            return True
    return False


class _NalOAuthProxy(OAuthProxy):
    """OAuthProxy that refuses foreign redirects at registration.

    FastMCP treats its patterns as an extension: a URI outside them still
    passes when the client registered it, and anyone may register. Here the
    registration itself is refused, and at /authorize only the registered
    URIs count. A client that registers without a scope gets every scope the
    server offers (FastMCP's default is none, so /authorize then fails).
    """

    def __init__(self, *, nal_redirects: tuple[str, ...], nal_scopes: list[str], **kw):
        super().__init__(allowed_client_redirect_uris=[], **kw)
        self._nal_redirects = nal_redirects
        self._nal_scopes = nal_scopes

    async def register_client(self, client_info):
        from mcp.server.auth.provider import RegistrationError

        bad = [
            str(u)
            for u in client_info.redirect_uris or []
            if not redirect_allowed(str(u), self._nal_redirects)
        ]
        if bad or not client_info.redirect_uris:
            raise RegistrationError(
                error="invalid_redirect_uri",
                error_description="redirect_uri is not one of this server's hosts",
            )
        if not client_info.scope:
            client_info.scope = " ".join(self._nal_scopes)
        await super().register_client(client_info)


def build_nal_oauth(
    *,
    base_url: str,
    client_id: str,
    scopes: list[str] | tuple[str, ...],
    users_url: str | None = None,
    redirects: tuple[str, ...] = ALL_HOST_REDIRECTS,
) -> OAuthProxy:
    """The OAuthProxy for one hosted server.

    ``base_url`` is the public URL the server is mounted at (the Kong route);
    nal-users must list ``{base_url}/auth/callback`` for ``client_id``.
    ``scopes`` are the server's own scopes; the OIDC ones are added because
    ChatGPT requests them by default.
    """
    users = (users_url or os.environ.get("NAL_USERS_API_URL") or USERS_URL).rstrip("/")
    verifier = JWTVerifier(
        jwks_uri=f"{users}/oauth/jwks",
        issuer=users,  # must equal the token 'iss' exactly
        # No audience: nal-users access tokens carry no 'aud'. No
        # required_scopes: nal-users writes a non-standard 'scopes' list claim
        # that JWTVerifier does not read.
    )
    return _NalOAuthProxy(
        nal_redirects=tuple(redirects),
        nal_scopes=[*OIDC_SCOPES, *scopes],
        upstream_authorization_endpoint=f"{users}/oauth/authorize",
        upstream_token_endpoint=f"{users}/oauth/token",
        upstream_client_id=client_id,
        # A public client: the parameter is mandatory, the value never sent.
        upstream_client_secret=os.environ.get("MCP_UPSTREAM_SECRET", "unused-public-client"),
        token_endpoint_auth_method="none",
        token_verifier=verifier,
        base_url=base_url.rstrip("/"),
        redirect_path="/auth/callback",
        forward_pkce=True,  # nal-users requires S256 on the upstream leg
        valid_scopes=[*OIDC_SCOPES, *scopes],
        # One upstream client serves every MCP client: the consent screen is
        # the defence against a confused deputy.
        require_authorization_consent=os.environ.get("MCP_PROXY_CONSENT", "true").strip().lower()
        != "false",
        # Stable, so reference tokens survive a restart.
        jwt_signing_key=os.environ.get("MCP_JWT_SIGNING_KEY"),
    )


def forwarded_bearer() -> str | None:
    """The caller's nal-users token for this request, or None.

    Hosted mode: the token behind the proxy's reference token. Without a
    request (stdio, tests) None, and the caller falls back to its own token.
    """
    try:
        from fastmcp.server.dependencies import get_access_token
    except ImportError:  # pragma: no cover - fastmcp is a consumer dependency
        return None
    token = get_access_token()
    return token.token if token is not None else None
