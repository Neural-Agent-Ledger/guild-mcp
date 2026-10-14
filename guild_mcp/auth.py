"""OAuth 2.0 authentication for NAL MCP servers.

Vendored from nal-mcp-sdk. Identical copy used across all NAL MCP servers.
Shared across all NAL MCP servers (social-graph, multi-agent, etc.).
Each server provides its own scopes; everything else (client_id, users_api,
token path) is shared so a single login covers all MCP servers.

Implements two flows against nal-users OAuth endpoints:

1. Authorization Code + PKCE (RFC 7636) for local interactive sessions.
2. Device Authorization Grant (RFC 8628) for headless / SSH / CI.

Public API:

    authenticate(scopes=["social-graph:read", ...]) -> (access_token, user_uuid)

Token cache (~/.nal/mcp-token, chmod 0o600) is shared across all MCP servers.
The cache is written atomically via os.replace() so concurrent MCP servers
that share the same cache cannot observe a torn JSON file.

Concurrent refreshes from a single process are serialized via a per-event-loop
asyncio.Lock so we never fire two refresh_token requests when N tool calls
discover expiry at the same time.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import json
import logging
import os
import secrets
import sys
import tempfile
import time
import webbrowser
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from threading import Thread
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse

import httpx

logger = logging.getLogger(__name__)

_HTTP_TIMEOUT: float = 15.0
_PKCE_CALLBACK_TIMEOUT: float = 300.0
_DEVICE_POLL_CEILING: float = 900.0


class AuthenticationRequired(Exception):  # noqa: N818 — name is public API across vendored copies
    """No valid credentials available and interactive auth is disabled.

    Raised in MCP / automated contexts where PKCE and Device flows
    cannot run.  The message contains user-facing instructions.
    """


@dataclass
class AuthConfig:
    """Configuration resolved once per authenticate() call."""

    client_id: str = ""
    scopes: list[str] = field(default_factory=list)
    users_api: str = ""
    token_path: Path = field(default_factory=lambda: Path.home() / ".nal" / "mcp-token")

    def __post_init__(self) -> None:
        if not self.client_id:
            self.client_id = os.environ.get("NAL_OAUTH_CLIENT_ID", "nal-mcp")
        if not self.scopes:
            env_scopes = os.environ.get("NAL_OAUTH_SCOPES", "")
            self.scopes = env_scopes.split() if env_scopes else []
        if not self.users_api:
            self.users_api = os.environ.get("NAL_USERS_API_URL", "http://localhost:8086").rstrip(
                "/"
            )


# --------------------------------------------------------------------------- #
# Token cache                                                                  #
# --------------------------------------------------------------------------- #


def _load_cached_token(cfg: AuthConfig) -> dict[str, Any] | None:
    if not cfg.token_path.exists():
        return None
    try:
        raw = cfg.token_path.read_text(encoding="utf-8")
        data = json.loads(raw)
    except (OSError, json.JSONDecodeError) as exc:
        logger.debug("Cached token unreadable: %s", exc)
        return None

    if not isinstance(data, dict):
        return None
    if "access_token" not in data:
        logger.info("Legacy token cache detected, discarding and re-authenticating.")
        return None
    return data


def _save_token(cfg: AuthConfig, payload: dict[str, Any]) -> None:
    """Write token cache atomically.

    Uses tmp-then-os.replace() so a concurrent reader (another MCP server
    sharing ``~/.nal/mcp-token``) sees either the old or the new payload,
    never a torn JSON. ``os.replace`` is atomic on POSIX and near-atomic
    on Windows NTFS (single-volume rename).
    """
    cfg.token_path.parent.mkdir(parents=True, exist_ok=True)
    serialized = json.dumps(payload)
    fd, tmp_name = tempfile.mkstemp(
        prefix=cfg.token_path.name + ".",
        suffix=".tmp",
        dir=str(cfg.token_path.parent),
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(serialized)
            fh.flush()
            with contextlib.suppress(OSError):
                os.fsync(fh.fileno())
        with contextlib.suppress(OSError):
            os.chmod(tmp_path, 0o600)
        os.replace(tmp_path, cfg.token_path)
    except Exception:
        with contextlib.suppress(OSError):
            tmp_path.unlink(missing_ok=True)
        raise
    with contextlib.suppress(OSError):
        cfg.token_path.chmod(0o600)
    logger.info("Token cache saved to %s", cfg.token_path)


def _delete_cached_token(cfg: AuthConfig) -> None:
    try:
        cfg.token_path.unlink(missing_ok=True)
    except OSError as exc:
        logger.debug("Could not remove token cache: %s", exc)


# --------------------------------------------------------------------------- #
# Environment detection                                                        #
# --------------------------------------------------------------------------- #


def _is_interactive() -> bool:
    if not sys.stdin.isatty():
        return False
    if os.environ.get("CI"):
        return False
    return not os.environ.get("CONTAINER")


# --------------------------------------------------------------------------- #
# Token validation and refresh                                                 #
# --------------------------------------------------------------------------- #


async def _validate(cfg: AuthConfig, access_token: str) -> str | None:
    try:
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
            resp = await client.get(
                f"{cfg.users_api}/me",
                headers={"Authorization": f"Bearer {access_token}"},
            )
    except httpx.HTTPError as exc:
        logger.debug("Token validation network error: %s", exc)
        return None

    if resp.status_code != 200:
        logger.debug("Token validation HTTP %s: %s", resp.status_code, resp.text[:200])
        return None

    try:
        body = resp.json()
    except ValueError:
        return None

    user_uuid = body.get("uuid") or body.get("user_uuid") or body.get("id")
    return str(user_uuid) if user_uuid is not None else None


async def _refresh(cfg: AuthConfig, refresh_token: str) -> dict[str, Any] | None:
    try:
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
            resp = await client.post(
                f"{cfg.users_api}/oauth/token",
                json={
                    "grant_type": "refresh_token",
                    "refresh_token": refresh_token,
                    "client_id": cfg.client_id,
                },
            )
    except httpx.HTTPError as exc:
        logger.debug("Refresh network error: %s", exc)
        return None

    if resp.status_code != 200:
        logger.debug("Refresh HTTP %s: %s", resp.status_code, resp.text[:200])
        return None

    try:
        data = resp.json()
    except ValueError:
        return None

    access_token = data.get("access_token")
    if not access_token:
        return None

    user_uuid = await _validate(cfg, access_token)
    if user_uuid is None:
        return None

    now = time.time()
    return {
        "access_token": access_token,
        "refresh_token": data.get("refresh_token") or refresh_token,
        "expires_at": now + float(data.get("expires_in", 3600)),
        "user_uuid": user_uuid,
        "obtained_at": now,
        "flow": "refresh",
    }


# --------------------------------------------------------------------------- #
# Locked refresh — single-flight per event loop                                #
# --------------------------------------------------------------------------- #
#
# Multiple coroutines in one process can race to refresh when they all observe
# expiry at the same time. We serialize via a module-private asyncio.Lock that
# is created lazily, bound to the running event loop. Other coroutines wait
# while one performs the refresh, then they pick up the new cached token.
#
# This lock is module-private — never exported — and rebinds itself if the
# event loop changes (e.g. asyncio.run() calls in PKCE/device flows that
# create short-lived loops are unaffected because they finish before the
# stdio loop starts).
#
# NOTE: cross-process serialization (multiple MCP servers refreshing at the
# same time) is intentionally NOT covered — atomic _save_token() ensures
# the cache file never tears, and an extra round-trip every few hours is
# cheap. Adding fcntl/msvcrt file locks would harm portability.

_refresh_lock: asyncio.Lock | None = None
_refresh_lock_loop: asyncio.AbstractEventLoop | None = None


def _get_refresh_lock() -> asyncio.Lock:
    """Lazily create / rebind the asyncio.Lock for the running loop."""
    global _refresh_lock, _refresh_lock_loop
    loop = asyncio.get_running_loop()
    if _refresh_lock is None or _refresh_lock_loop is not loop:
        _refresh_lock = asyncio.Lock()
        _refresh_lock_loop = loop
    return _refresh_lock


async def refresh_with_lock(cfg: AuthConfig, refresh_token: str) -> dict[str, Any] | None:
    """Acquire the per-loop refresh lock, refresh, and persist the new token.

    Public-but-internal: used by NalMcpClient on 401. The lock guarantees
    that N concurrent callers triggered by a burst of 401s will only do
    ONE refresh round-trip; the rest wait and re-read the cache.

    Returns the new payload dict on success, or None on failure.
    """
    lock = _get_refresh_lock()
    async with lock:
        # Re-read cache: a sibling coroutine may have refreshed already.
        cached = _load_cached_token(cfg)
        if cached is not None:
            current_refresh = cached.get("refresh_token") or refresh_token
            # If access_token is still good for >60s, use it.
            if cached.get("expires_at", 0) > time.time() + 60 and cached.get("access_token"):
                return cached
        else:
            current_refresh = refresh_token

        if not current_refresh:
            return None

        refreshed = await _refresh(cfg, current_refresh)
        if refreshed is not None:
            _save_token(cfg, refreshed)
        return refreshed


# --------------------------------------------------------------------------- #
# PKCE helpers                                                                 #
# --------------------------------------------------------------------------- #


def _compute_pkce_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def _verify_pkce_challenge(verifier: str, challenge: str) -> bool:
    expected = _compute_pkce_challenge(verifier)
    return secrets.compare_digest(expected, challenge)


def _make_callback_handler(
    captured: dict[str, str],
) -> type[BaseHTTPRequestHandler]:
    class _CallbackHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            if parsed.path != "/callback":
                self.send_response(404)
                self.end_headers()
                return

            params = parse_qs(parsed.query)
            for key in ("code", "state", "error", "error_description"):
                value = params.get(key, [None])[0]
                if value is not None:
                    captured[key] = value

            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            if "code" in captured:
                body = (
                    b'<html><body style="font-family:system-ui;'
                    b'text-align:center;padding:60px">'
                    b"<h1>NAL MCP authorized</h1>"
                    b"<p>You can close this window and return to the terminal.</p>"
                    b"<script>setTimeout(function(){window.close();},1500);</script>"
                    b"</body></html>"
                )
            else:
                err = captured.get("error", "unknown_error")
                body = (
                    b'<html><body style="font-family:system-ui;'
                    b'text-align:center;padding:60px">'
                    b"<h1>Authorization failed</h1><p>"
                    + err.encode("ascii", "replace")
                    + b"</p></body></html>"
                )
            self.wfile.write(body)

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            return

    return _CallbackHandler


# --------------------------------------------------------------------------- #
# PKCE flow                                                                    #
# --------------------------------------------------------------------------- #


def _pkce_flow(cfg: AuthConfig) -> dict[str, Any] | None:
    verifier = secrets.token_urlsafe(64)
    challenge = _compute_pkce_challenge(verifier)
    state = secrets.token_urlsafe(16)

    captured: dict[str, str] = {}
    handler_cls = _make_callback_handler(captured)
    try:
        srv = HTTPServer(("127.0.0.1", 0), handler_cls)
    except OSError as exc:
        print(
            f"Authentication failed: cannot bind local callback server: {exc}",
            file=sys.stderr,
            flush=True,
        )
        return None

    port = srv.server_address[1]
    redirect_uri = f"http://127.0.0.1:{port}/callback"

    params = {
        "client_id": cfg.client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "scope": " ".join(cfg.scopes),
        "state": state,
    }
    authorize_url = f"{cfg.users_api}/oauth/authorize?{urlencode(params)}"

    print("NAL MCP authentication (PKCE)", file=sys.stderr, flush=True)
    print(f"  Opening browser: {authorize_url}", file=sys.stderr, flush=True)

    try:
        webbrowser.open(authorize_url)
    except Exception:
        print(
            f"  Could not open browser automatically, visit manually: {authorize_url}",
            file=sys.stderr,
            flush=True,
        )

    srv.timeout = _PKCE_CALLBACK_TIMEOUT
    thread = Thread(target=srv.handle_request, daemon=True)
    thread.start()
    thread.join(timeout=_PKCE_CALLBACK_TIMEOUT)
    with contextlib.suppress(OSError):
        srv.server_close()

    if thread.is_alive():
        print(
            f"Authentication failed: browser callback timed out after "
            f"{int(_PKCE_CALLBACK_TIMEOUT)}s",
            file=sys.stderr,
            flush=True,
        )
        return None

    if "error" in captured:
        print(f"Authentication failed: {captured['error']}", file=sys.stderr, flush=True)
        return None

    if captured.get("state") != state:
        print(
            "Authentication failed: OAuth state mismatch (possible CSRF)",
            file=sys.stderr,
            flush=True,
        )
        return None

    code = captured.get("code")
    if not code:
        print(
            "Authentication failed: callback did not deliver an authorization code",
            file=sys.stderr,
            flush=True,
        )
        return None

    try:
        resp = httpx.post(
            f"{cfg.users_api}/oauth/token",
            json={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": redirect_uri,
                "client_id": cfg.client_id,
                "code_verifier": verifier,
            },
            timeout=_HTTP_TIMEOUT,
        )
    except httpx.HTTPError as exc:
        print(
            f"Authentication failed: token exchange network error: {exc}",
            file=sys.stderr,
            flush=True,
        )
        return None

    if resp.status_code != 200:
        print(
            f"Authentication failed: token exchange HTTP {resp.status_code}: {resp.text[:200]}",
            file=sys.stderr,
            flush=True,
        )
        return None

    try:
        data = resp.json()
    except ValueError:
        print(
            "Authentication failed: token endpoint returned non-JSON body",
            file=sys.stderr,
            flush=True,
        )
        return None

    access_token = data.get("access_token")
    if not access_token:
        print(
            "Authentication failed: token response missing access_token",
            file=sys.stderr,
            flush=True,
        )
        return None

    user_uuid = asyncio.run(_validate(cfg, access_token))
    if user_uuid is None:
        print(
            "Authentication failed: cannot validate freshly issued token",
            file=sys.stderr,
            flush=True,
        )
        return None

    now = time.time()
    return {
        "access_token": access_token,
        "refresh_token": data.get("refresh_token", ""),
        "expires_at": now + float(data.get("expires_in", 3600)),
        "user_uuid": user_uuid,
        "obtained_at": now,
        "flow": "pkce",
    }


# --------------------------------------------------------------------------- #
# Device Authorization Grant flow                                              #
# --------------------------------------------------------------------------- #


def _device_flow(cfg: AuthConfig) -> dict[str, Any] | None:
    try:
        resp = httpx.post(
            f"{cfg.users_api}/oauth/device",
            json={"client_id": cfg.client_id, "scope": " ".join(cfg.scopes)},
            timeout=_HTTP_TIMEOUT,
        )
    except httpx.HTTPError as exc:
        print(
            f"Authentication failed: device flow init network error: {exc}",
            file=sys.stderr,
            flush=True,
        )
        return None

    if resp.status_code != 200:
        print(
            f"Authentication failed: device flow init HTTP {resp.status_code}: {resp.text[:200]}",
            file=sys.stderr,
            flush=True,
        )
        return None

    try:
        data = resp.json()
    except ValueError:
        print(
            "Authentication failed: device flow init returned non-JSON body",
            file=sys.stderr,
            flush=True,
        )
        return None

    device_code = data.get("device_code")
    user_code = data.get("user_code")
    verification_uri = data.get("verification_uri", "")
    verification_uri_complete = data.get("verification_uri_complete", verification_uri)
    expires_in = float(data.get("expires_in", 600))
    interval = float(data.get("interval", 5))

    if not device_code or not user_code:
        print(
            "Authentication failed: device flow response missing required fields",
            file=sys.stderr,
            flush=True,
        )
        return None

    separator = "=" * 60
    print(separator, file=sys.stderr, flush=True)
    print("NAL MCP Authentication Required", file=sys.stderr, flush=True)
    print("", file=sys.stderr, flush=True)
    print(f"  Visit:  {verification_uri}", file=sys.stderr, flush=True)
    print(f"  Code:   {user_code}", file=sys.stderr, flush=True)
    print("", file=sys.stderr, flush=True)
    print(f"  Or open directly: {verification_uri_complete}", file=sys.stderr, flush=True)
    print(f"  Code expires in {int(expires_in)} seconds", file=sys.stderr, flush=True)
    print(separator, file=sys.stderr, flush=True)

    poll_deadline = time.time() + min(expires_in, _DEVICE_POLL_CEILING)
    while time.time() < poll_deadline:
        time.sleep(interval)
        try:
            poll_resp = httpx.post(
                f"{cfg.users_api}/oauth/token",
                json={
                    "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                    "device_code": device_code,
                    "client_id": cfg.client_id,
                },
                timeout=_HTTP_TIMEOUT,
            )
        except httpx.HTTPError:
            continue

        if poll_resp.status_code == 200:
            try:
                body = poll_resp.json()
            except ValueError:
                print(
                    "Authentication failed: token endpoint returned non-JSON body",
                    file=sys.stderr,
                    flush=True,
                )
                return None

            access_token = body.get("access_token")
            if not access_token:
                print(
                    "Authentication failed: token response missing access_token",
                    file=sys.stderr,
                    flush=True,
                )
                return None

            user_uuid = asyncio.run(_validate(cfg, access_token))
            if user_uuid is None:
                print(
                    "Authentication failed: cannot validate freshly issued token",
                    file=sys.stderr,
                    flush=True,
                )
                return None

            now = time.time()
            return {
                "access_token": access_token,
                "refresh_token": body.get("refresh_token", ""),
                "expires_at": now + float(body.get("expires_in", 3600)),
                "user_uuid": user_uuid,
                "obtained_at": now,
                "flow": "device",
            }

        try:
            err_body = poll_resp.json()
        except ValueError:
            err_body = {}
        err = err_body.get("error", "")

        if err == "authorization_pending":
            continue
        if err == "slow_down":
            interval += 5.0
            continue
        if err in ("access_denied", "expired_token"):
            print(f"Authentication failed: device flow {err}", file=sys.stderr, flush=True)
            return None

        print(
            f"Authentication failed: unexpected device flow error "
            f"({poll_resp.status_code}) {err or poll_resp.text[:200]}",
            file=sys.stderr,
            flush=True,
        )
        return None

    print(
        "Authentication failed: device code expired before approval",
        file=sys.stderr,
        flush=True,
    )
    return None


# --------------------------------------------------------------------------- #
# Public entry point                                                           #
# --------------------------------------------------------------------------- #


def authenticate(
    *,
    scopes: list[str] | None = None,
    client_id: str | None = None,
    users_api: str | None = None,
    token_path: Path | None = None,
    interactive: bool | None = None,
) -> tuple[str, str]:
    """Authenticate against nal-users and return (access_token, user_uuid).

    Each MCP server provides its own scopes; the rest of the config
    is shared so one login covers all servers.

    Resolution order:
    1. NAL_API_TOKEN env var (CI override, no OAuth dance).
    2. Cached token from ~/.nal/mcp-token, with silent refresh if expired.
    3. PKCE flow (interactive TTY, browser available).
    4. Device Authorization Grant (headless / SSH / CI).

    Args:
        interactive: Controls whether browser/device-code flows are attempted.
            True  — always allow interactive flows.
            False — only env var + cached token; raise AuthenticationRequired
                    if neither works (MCP / automated mode).
            None  — auto-detect via _is_interactive() (default, legacy).

    On unrecoverable failure: prints to stderr and calls sys.exit(1),
    unless interactive=False in which case AuthenticationRequired is raised.
    """
    cfg = AuthConfig()
    if scopes is not None:
        cfg.scopes = scopes
    if client_id is not None:
        cfg.client_id = client_id
    if users_api is not None:
        cfg.users_api = users_api
    if token_path is not None:
        cfg.token_path = token_path

    # 1. CI override.
    env_token = os.environ.get("NAL_API_TOKEN", "")
    if env_token:
        user_uuid = asyncio.run(_validate(cfg, env_token))
        if user_uuid:
            return env_token, user_uuid
        print(
            "NAL_API_TOKEN is set but invalid or expired, trying other methods.",
            file=sys.stderr,
            flush=True,
        )

    # 2. Cached token.
    cached = _load_cached_token(cfg)
    if cached is not None:
        if cached.get("expires_at", 0) > time.time() + 60:
            user_uuid = asyncio.run(_validate(cfg, cached["access_token"]))
            if user_uuid:
                return cached["access_token"], user_uuid
        refresh_token = cached.get("refresh_token") or ""
        if refresh_token:
            refreshed = asyncio.run(_refresh(cfg, refresh_token))
            if refreshed is not None:
                _save_token(cfg, refreshed)
                return refreshed["access_token"], refreshed["user_uuid"]
        _delete_cached_token(cfg)

    # 3. Interactive vs headless.
    if interactive is False:
        raise AuthenticationRequired(
            "No valid token found. Either:\n"
            "  - Set NAL_API_TOKEN env var in .mcp.json, or\n"
            "  - Run the server's login command once (guild-mcp login)\n"
            "    to authenticate interactively (token is cached for future runs)."
        )

    _interactive = _is_interactive() if interactive is None else interactive
    result: dict[str, Any] | None = _pkce_flow(cfg) if _interactive else _device_flow(cfg)

    if result is None:
        print("Authentication failed: no token obtained", file=sys.stderr, flush=True)
        sys.exit(1)

    _save_token(cfg, result)
    return result["access_token"], result["user_uuid"]


# --------------------------------------------------------------------------- #
# Refresh callback factory — used by NalMcpClient via M-3 wiring               #
# --------------------------------------------------------------------------- #


def make_refresh_callback(
    cfg: AuthConfig | None = None,
) -> Callable[[], Awaitable[str | None]]:
    """Build an async refresh callback bound to a config.

    The callback re-reads the cached token, calls the refresh endpoint
    under an asyncio.Lock (single-flight), persists the new payload,
    and returns the fresh access_token (or None on failure).

    Use this as the ``refresh_callback`` kwarg to NalMcpClient.
    """
    bound_cfg = cfg or AuthConfig()

    async def _cb() -> str | None:
        cached = _load_cached_token(bound_cfg)
        if cached is None:
            return None
        refresh_token = cached.get("refresh_token") or ""
        if not refresh_token:
            return None
        refreshed = await refresh_with_lock(bound_cfg, refresh_token)
        if refreshed is None:
            return None
        return refreshed.get("access_token")

    return _cb


if __name__ == "__main__":
    # CLI entry point: interactive login, caches token for MCP servers.
    token, uid = authenticate(interactive=True)
    print(f"Authenticated as {uid}, token cached at ~/.nal/mcp-token")
