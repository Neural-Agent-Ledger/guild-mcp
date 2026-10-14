"""Unit tests for auth — vendored from nal-mcp-sdk.

Covers the pure logic parts we can exercise without a backend:

- PKCE challenge generation and constant-time verification
- Token cache read / write / legacy-shape detection
- Interactive environment detection gating
"""

from __future__ import annotations

import importlib
import json
import os
import sys
from pathlib import Path
from typing import Any

import pytest


@pytest.fixture
def auth_module(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    """Reload guild_mcp.auth with the token path redirected into a tmp dir."""
    # Ensure env knobs are in a known state before import.
    for var in (
        "NAL_API_TOKEN",
        "NAL_OAUTH_CLIENT_ID",
        "NAL_OAUTH_SCOPES",
        "NAL_USERS_API_URL",
        "CI",
        "CONTAINER",
    ):
        monkeypatch.delenv(var, raising=False)

    # Put the fake HOME in tmp so ~/.nal/mcp-token lands in tmp_path.
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))  # Windows equivalent

    # Force a fresh import so module-level defaults are recomputed.
    sys.modules.pop("guild_mcp.auth", None)
    module = importlib.import_module("guild_mcp.auth")
    return module


# --------------------------------------------------------------------------- #
# PKCE math                                                                    #
# --------------------------------------------------------------------------- #


def test_pkce_challenge_roundtrip(auth_module: Any) -> None:
    verifier = "this-is-a-fake-verifier-" + "x" * 60
    challenge = auth_module._compute_pkce_challenge(verifier)
    assert auth_module._verify_pkce_challenge(verifier, challenge) is True


def test_pkce_challenge_wrong_verifier_fails(auth_module: Any) -> None:
    challenge = auth_module._compute_pkce_challenge("correct-verifier-abc")
    assert auth_module._verify_pkce_challenge("wrong-verifier-abc", challenge) is False


def test_pkce_challenge_is_base64url_nopad(auth_module: Any) -> None:
    challenge = auth_module._compute_pkce_challenge("sample-verifier")
    # base64url alphabet only, no padding characters.
    assert "=" not in challenge
    assert "+" not in challenge
    assert "/" not in challenge
    # sha256 -> 32 bytes -> 43 chars base64url-nopad.
    assert len(challenge) == 43


# --------------------------------------------------------------------------- #
# Token cache                                                                  #
# --------------------------------------------------------------------------- #


def test_load_cached_token_missing_file_returns_none(auth_module: Any, tmp_path: Path) -> None:
    cfg = auth_module.AuthConfig(token_path=tmp_path / ".nal" / "mcp-token")
    assert auth_module._load_cached_token(cfg) is None


def test_save_and_load_new_format_roundtrip(auth_module: Any, tmp_path: Path) -> None:
    cfg = auth_module.AuthConfig(token_path=tmp_path / ".nal" / "mcp-token")
    payload = {
        "access_token": "access-123",
        "refresh_token": "refresh-456",
        "expires_at": 9999999999,
        "user_uuid": "u-uuid",
        "obtained_at": 1,
        "flow": "pkce",
    }
    auth_module._save_token(cfg, payload)
    loaded = auth_module._load_cached_token(cfg)
    assert loaded == payload


def test_load_legacy_format_returns_none(auth_module: Any, tmp_path: Path) -> None:
    cfg = auth_module.AuthConfig(token_path=tmp_path / ".nal" / "mcp-token")
    # Legacy shape from the pre-OAuth version of auth.py.
    cfg.token_path.parent.mkdir(parents=True, exist_ok=True)
    cfg.token_path.write_text(
        json.dumps({"token": "legacy", "user_id": "x", "source": "browser_login"}),
        encoding="utf-8",
    )
    assert auth_module._load_cached_token(cfg) is None


def test_load_garbage_json_returns_none(auth_module: Any, tmp_path: Path) -> None:
    cfg = auth_module.AuthConfig(token_path=tmp_path / ".nal" / "mcp-token")
    cfg.token_path.parent.mkdir(parents=True, exist_ok=True)
    cfg.token_path.write_text("not json at all", encoding="utf-8")
    assert auth_module._load_cached_token(cfg) is None


def test_delete_cached_token_is_idempotent(auth_module: Any, tmp_path: Path) -> None:
    cfg = auth_module.AuthConfig(token_path=tmp_path / ".nal" / "mcp-token")
    # Missing file: no crash.
    auth_module._delete_cached_token(cfg)
    # Present file: removed.
    auth_module._save_token(
        cfg,
        {
            "access_token": "a",
            "refresh_token": "r",
            "expires_at": 0,
            "user_uuid": "u",
            "obtained_at": 0,
            "flow": "device",
        },
    )
    assert cfg.token_path.exists()
    auth_module._delete_cached_token(cfg)
    assert not cfg.token_path.exists()


# --------------------------------------------------------------------------- #
# Interactive detection                                                        #
# --------------------------------------------------------------------------- #


def test_interactive_false_without_tty(auth_module: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    assert auth_module._is_interactive() is False


def test_interactive_false_under_ci(auth_module: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setenv("CI", "true")
    assert auth_module._is_interactive() is False


def test_interactive_false_in_container(auth_module: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setenv("CONTAINER", "docker")
    assert auth_module._is_interactive() is False


def test_interactive_true_with_tty_and_no_ci(
    auth_module: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.delenv("CONTAINER", raising=False)
    assert auth_module._is_interactive() is True


# --------------------------------------------------------------------------- #
# SDK AuthConfig defaults                                                      #
# --------------------------------------------------------------------------- #


def test_default_client_id(auth_module: Any) -> None:
    cfg = auth_module.AuthConfig()
    assert cfg.client_id == "nal-mcp"


def test_default_scopes_empty(auth_module: Any) -> None:
    """SDK default scopes are empty — each MCP server provides its own."""
    cfg = auth_module.AuthConfig()
    assert cfg.scopes == []


def test_env_overrides_client_id(auth_module: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NAL_OAUTH_CLIENT_ID", "custom-client")
    monkeypatch.setenv("NAL_OAUTH_SCOPES", "scope-a scope-b")
    cfg = auth_module.AuthConfig()
    assert cfg.client_id == "custom-client"
    assert cfg.scopes == ["scope-a", "scope-b"]


# --------------------------------------------------------------------------- #
# authenticate function                                                        #
# --------------------------------------------------------------------------- #


def test_authenticate_is_callable(auth_module: Any) -> None:
    """authenticate exists and is callable."""
    assert callable(auth_module.authenticate)


def test_authentication_required_exception_exists(auth_module: Any) -> None:
    """AuthenticationRequired should be importable and be an Exception."""
    assert hasattr(auth_module, "AuthenticationRequired")
    assert issubclass(auth_module.AuthenticationRequired, Exception)


def test_authenticate_interactive_false_raises_without_token(
    auth_module: Any, tmp_path: Path
) -> None:
    """With interactive=False and no env/cache, authenticate must raise."""
    cfg_path = tmp_path / ".nal" / "mcp-token"
    with pytest.raises(auth_module.AuthenticationRequired):
        auth_module.authenticate(
            token_path=cfg_path,
            users_api="http://localhost:1",  # unreachable
            interactive=False,
        )
