"""HTTP client for NAL MCP servers.

Vendored from nal-mcp-sdk. Identical copy used across all NAL MCP servers.

Provides NalMcpClient — a thin wrapper around httpx.AsyncClient that adds:

- Connection pooling with configurable limits.
- Circuit breaker (auto-open after N consecutive failures, cooldown period).
- Bearer-token auth header. ``X-User-Id`` / ``X-User-Type`` are NOT sent —
  Kong derives ``X-User-ID`` from the JWT ``sub`` claim and clears any client
  value, so the client header was always dead weight. Removing them now is
  forward-compatible with the in-flight Kong change that will also derive
  ``X-User-Type`` from the JWT.
- Silent token refresh on HTTP 401: the client invokes a caller-supplied
  ``refresh_callback`` (Awaitable[str | None]) and retries the original
  request once with the new token. If refresh succeeds, the 401 does NOT
  count against the circuit breaker — only a real backend outage trips it.
- Proactive expiry refresh: if the caller supplied an ``expires_at`` value,
  the client refreshes 60s before expiry to avoid a guaranteed-fail trip.
"""

from __future__ import annotations

import logging
import os
import time
from collections.abc import Awaitable, Callable

import httpx

_TRUTHY = ("1", "true", "yes", "on")


def _readonly() -> bool:
    """True when the operator asked for a session that cannot write.

    Read per call rather than captured at import so a test -- or a host
    that sets the variable after this module loads -- is honoured.

    Two names: ``NAL_MCP_READONLY`` is the generic one to carry upstream
    into nal-mcp-sdk, ``GUILD_GRAPH_READONLY`` is what this server's docs
    and deployment already use. Either is enough.
    """
    return any(
        os.environ.get(name, "").strip().lower() in _TRUTHY
        for name in ("NAL_MCP_READONLY", "GUILD_GRAPH_READONLY")
    )


logger = logging.getLogger(__name__)

# Refresh window: pre-empt expiry this many seconds early.
_EXPIRY_SKEW_SECONDS: float = 60.0


class NalMcpClient:
    """Authenticated, pooled HTTP client with circuit breaker."""

    def __init__(
        self,
        base_url: str,
        token: str,
        user_id: str,
        *,
        user_type: str = "agent",
        timeout: float = 15.0,
        connect_timeout: float = 5.0,
        max_connections: int = 50,
        max_keepalive: int = 20,
        cb_threshold: int = 5,
        cb_cooldown: float = 30.0,
        refresh_callback: Callable[[], Awaitable[str | None]] | None = None,
        expires_at: float | None = None,
        error_sanitizer: Callable[[str], str] | None = None,
    ) -> None:
        """Construct an authenticated client.

        Args:
            base_url: API base URL passed to httpx.AsyncClient.
            token: Initial bearer token.
            user_id: User UUID string. Kept for backward compat / logging
                only — it's no longer sent over the wire (Kong derives it
                from JWT sub). Empty string is allowed.
            user_type: Kept for backward compat. No longer sent over the
                wire — Kong is moving to derive this from the JWT.
            timeout: Per-request timeout (seconds).
            connect_timeout: Connection timeout (seconds).
            max_connections: httpx pool ceiling.
            max_keepalive: httpx keep-alive pool ceiling.
            cb_threshold: Consecutive backend failures before the breaker opens.
            cb_cooldown: Seconds the breaker stays open before the next probe.
            refresh_callback: Optional async callable returning the new
                access_token (or None on failure). Used for silent refresh
                on 401 and proactive pre-expiry refresh. The callback is
                responsible for taking any locks; this client just calls it.
            expires_at: Optional unix timestamp at which the current token
                expires. When set, the client refreshes pre-emptively if
                ``time.time() > expires_at - 60``.
            error_sanitizer: Optional callable applied to a non-2xx response
                body before it is raised. The error path is how backend text
                reaches the model when a call fails, so a server that fences
                untrusted content must fence it here too; injected rather
                than imported so this module stays generic enough to
                re-vendor from nal-mcp-sdk unchanged.
        """
        self._base_url = base_url
        self._token = token
        self._user_id = user_id
        self._user_type = user_type
        self._refresh_callback = refresh_callback
        self._expires_at = expires_at
        self._error_sanitizer = error_sanitizer

        self._client = httpx.AsyncClient(
            base_url=base_url,
            timeout=httpx.Timeout(timeout, connect=connect_timeout),
            limits=httpx.Limits(
                max_connections=max_connections,
                max_keepalive_connections=max_keepalive,
                keepalive_expiry=30.0,
            ),
        )

        # Circuit breaker state
        self._cb_failures = 0
        self._cb_last_failure = 0.0
        self._cb_threshold = cb_threshold
        self._cb_cooldown = cb_cooldown

    # -- Token lifecycle -------------------------------------------------------

    def update_token(self, token: str, expires_at: float | None = None) -> None:
        """Replace the bearer token in place.

        Used after a successful refresh so the next request sends the
        new token without recreating the client.
        """
        self._token = token
        if expires_at is not None:
            self._expires_at = expires_at

    def headers(self) -> dict[str, str]:
        """Build auth headers from the current token.

        Only ``Authorization: Bearer <token>`` is emitted. ``X-User-Id`` and
        ``X-User-Type`` are intentionally NOT sent — Kong derives both from
        the JWT and would clear/overwrite anything we provided.
        """
        if not self._token:
            raise ValueError(
                "Not authenticated. Set NAL_API_TOKEN environment variable "
                "with your API key from NAL Account (/me/apikey)."
            )
        return {"Authorization": f"Bearer {self._token}"}

    async def _maybe_proactive_refresh(self) -> None:
        """If we know the expiry, refresh ~60s before it lapses."""
        if self._refresh_callback is None or self._expires_at is None:
            return
        if time.time() <= self._expires_at - _EXPIRY_SKEW_SECONDS:
            return
        new_token = await self._refresh_callback()
        if new_token:
            self._token = new_token
            # We don't know the new expiry without round-tripping; clear
            # so we don't loop. The refresh callback is expected to update
            # the cache; the next request will trust the new token until
            # the server says otherwise.
            self._expires_at = None
        else:
            logger.debug("Proactive refresh returned no token; will let request 401")

    # -- Circuit breaker -------------------------------------------------------

    def check_circuit_breaker(self) -> None:
        """Raise if circuit breaker is open (too many consecutive failures)."""
        if (
            self._cb_failures >= self._cb_threshold
            and time.time() - self._cb_last_failure < self._cb_cooldown
        ):
            remaining = int(self._cb_cooldown - (time.time() - self._cb_last_failure))
            raise ValueError(
                f"Circuit breaker OPEN: backend unavailable "
                f"({self._cb_failures} failures, retry in {remaining}s)"
            )

    def record_success(self) -> None:
        self._cb_failures = 0

    def record_failure(self) -> None:
        self._cb_failures += 1
        self._cb_last_failure = time.time()
        logger.warning("Circuit breaker: failure %d/%d", self._cb_failures, self._cb_threshold)

    # -- HTTP methods ----------------------------------------------------------

    async def request(
        self,
        method: str,
        path: str,
        *,
        json: dict | list | None = None,
        params: dict | None = None,
    ) -> httpx.Response:
        """Make an authenticated request with circuit breaker and silent refresh.

        Behaviour:
        1. Check the circuit breaker (raise if open).
        2. If we have an expiry hint and we're within 60s of it, refresh first.
        3. Send the request.
        4. On HTTP 401, if a refresh_callback is configured, refresh and
           retry once with the new token. A 401 that is recovered does NOT
           trip the circuit breaker — it's auth state, not a backend outage.
        5. On other 4xx/5xx, raise ValueError (and tally for the breaker).
        """
        # Reads always pass. A non-GET is a state change, and when the
        # operator has asked for a read-only session it is refused here --
        # at the one place every tool funnels through -- rather than at
        # each write tool, where the 38th one would eventually be missed.
        if method.upper() != "GET" and _readonly():
            raise PermissionError(
                f"Read-only session (NAL_MCP_READONLY / GUILD_GRAPH_READONLY): "
                f"refusing {method} {path}."
            )

        self.check_circuit_breaker()
        await self._maybe_proactive_refresh()

        resp = await self._client.request(
            method,
            path,
            json=json,
            params=params,
            headers=self.headers(),
        )

        # M-1: silent refresh on 401, retry once.
        if resp.status_code == 401 and self._refresh_callback is not None:
            logger.debug("Got 401 on %s %s; attempting silent refresh", method, path)
            new_token = await self._refresh_callback()
            if new_token:
                self._token = new_token
                self._expires_at = None  # Trust new token until server pushes back.
                resp = await self._client.request(
                    method,
                    path,
                    json=json,
                    params=params,
                    headers=self.headers(),
                )
                if resp.status_code < 400:
                    # Recovered: do NOT count this round-trip as CB failure.
                    self.record_success()
                    return resp
                # Retry also failed — fall through to normal failure path.
                logger.warning(
                    "Silent refresh succeeded but retry returned %s on %s %s",
                    resp.status_code,
                    method,
                    path,
                )
            else:
                logger.warning("Silent refresh failed on 401 for %s %s", method, path)

        if resp.status_code >= 400:
            self.record_failure()
            # Truncated, then handed to the sanitizer. This path is how
            # content reaches the agent when a call FAILS, and it used to
            # bypass spotlighting entirely: FastMCP's `mask_error_details`
            # defaults to False, so the body lands in the model's context
            # verbatim. FastAPI's default 422 handler echoes the rejected
            # `input` back, which turned an over-length field into a way to
            # move already-marked text out of its fence and into
            # framework-authored error text.
            detail = resp.text[:500]
            if self._error_sanitizer is not None:
                detail = self._error_sanitizer(detail)
            raise ValueError(f"API error {resp.status_code}: {detail}")
        self.record_success()
        return resp

    async def get(self, path: str, **kw) -> httpx.Response:
        return await self.request("GET", path, **kw)

    async def post(self, path: str, **kw) -> httpx.Response:
        return await self.request("POST", path, **kw)

    async def put(self, path: str, **kw) -> httpx.Response:
        return await self.request("PUT", path, **kw)

    async def patch(self, path: str, **kw) -> httpx.Response:
        return await self.request("PATCH", path, **kw)

    async def delete(self, path: str, **kw) -> httpx.Response:
        return await self.request("DELETE", path, **kw)

    async def close(self) -> None:
        if self._client and not self._client.is_closed:
            await self._client.aclose()
