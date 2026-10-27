"""Tool metadata the plugin review checks, in one place.

The OpenAI plugin review (developers.openai.com/plugins/plugin-guidelines)
requires on every tool explicit booleans for ``readOnlyHint``,
``destructiveHint`` and ``openWorldHint``, a justification for each, unique
descriptive names, and per-tool ``securitySchemes``. The scan imports them
from the server, so a justification cannot fix a wrong hint: the hint itself
must be right. ``audit_tools`` is the gate each server runs in its tests.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

HINTS = ("readOnlyHint", "destructiveHint", "openWorldHint")


def hints(*, read_only: bool, destructive: bool, open_world: bool) -> dict[str, bool]:
    """The three hints, all explicit.

    read_only: only fetches, looks up, lists or previews.
    destructive: can delete, overwrite, cancel, revoke or send irreversibly,
        also through a default or an indirect effect. Undoable is not enough
        to say False.
    open_world: reaches the public internet or open-ended parties. A bounded
        private account is False even when hosted elsewhere.
    """
    if read_only and destructive:
        raise ValueError("a read-only tool cannot be destructive")
    return {"readOnlyHint": read_only, "destructiveHint": destructive, "openWorldHint": open_world}


def security(*schemes: str, scopes: tuple[str, ...] = ()) -> list[dict[str, Any]]:
    """Per-tool ``securitySchemes``: ``"noauth"``, ``"oauth2"`` or both.

    Both means the tool runs signed out and does more once the account is
    linked; the server must still check the token on every call that uses it.
    """
    out: list[dict[str, Any]] = []
    for s in schemes:
        if s == "noauth":
            out.append({"type": "noauth"})
        elif s == "oauth2":
            out.append({"type": "oauth2", "scopes": list(scopes)})
        else:
            raise ValueError(f"unknown security scheme {s!r}")
    return out


def tool_meta(
    *,
    schemes: list[dict[str, Any]],
    invoking: str | None = None,
    invoked: str | None = None,
    widget: str | None = None,
) -> dict[str, Any]:
    """``_meta`` for a tool: security, status labels, and the linked widget.

    The widget is named both ways: ``ui.resourceUri`` (MCP Apps) and
    ``openai/outputTemplate`` (ChatGPT's alias).
    """
    meta: dict[str, Any] = {"securitySchemes": schemes}
    if invoking:
        meta["openai/toolInvocation/invoking"] = invoking
    if invoked:
        meta["openai/toolInvocation/invoked"] = invoked
    if widget:
        meta["ui"] = {"resourceUri": widget}
        meta["openai/outputTemplate"] = widget
    return meta


def reviewed_tool(
    register: Any,
    *,
    prefix: str,
    open_world: bool,
    scopes: dict[str, tuple[str, ...]],
) -> Any:
    """Wrap ``mcp.tool`` so every tool leaves with what the review checks.

    Each tool still decides ``readOnlyHint`` itself (no default: reading is a
    per-tool fact). A write that does not say ``destructiveHint`` is taken as
    destructive, the safe side. ``openWorldHint`` is the server's, the title
    is made from the name, and ``securitySchemes`` take the scopes of the
    tool's "read"/"write" tag, or of its read-only hint when it has no tag.
    """

    def factory(
        *,
        tags: set[str] | None = None,
        annotations: dict | None = None,
        meta: dict | None = None,
        **kw,
    ):
        ann = dict(annotations or {})
        read_only = ann.get("readOnlyHint")
        if type(read_only) is not bool:
            raise ValueError("readOnlyHint must be decided per tool")
        ann.setdefault("destructiveHint", not read_only)
        ann.setdefault("openWorldHint", open_world)
        tag_set = set(tags or ())
        access = (
            "write"
            if "write" in tag_set
            else "read"
            if "read" in tag_set
            else "read"
            if read_only
            else "write"
        )

        def deco(fn: Any) -> Any:
            ann.setdefault("title", fn.__name__.removeprefix(prefix).replace("_", " ").capitalize())
            m = {"securitySchemes": security("oauth2", scopes=scopes[access]), **(meta or {})}
            return register(tags=tag_set or None, annotations=ann, meta=m, **kw)(fn)

        return deco

    return factory


@dataclass(frozen=True)
class Problem:
    tool: str
    what: str

    def __str__(self) -> str:
        return f"{self.tool}: {self.what}"


def audit_tools(tools: list[Any], *, prefix: str) -> list[Problem]:
    """Every review rule a scan can see, for MCP ``Tool`` objects.

    ``tools`` is what the server lists (``await mcp._list_tools_mcp()`` or the
    client's ``list_tools()``); an empty result is a pass.
    """
    problems: list[Problem] = []
    seen: set[str] = set()
    for t in tools:
        name = t.name
        if name in seen:
            problems.append(Problem(name, "listed twice"))
        seen.add(name)
        if not re.fullmatch(rf"{re.escape(prefix)}[a-z0-9_]+", name):
            problems.append(Problem(name, f"name must be {prefix}snake_case"))
        ann = t.annotations.model_dump(exclude_none=True) if t.annotations else {}
        for h in HINTS:
            if type(ann.get(h)) is not bool:
                problems.append(Problem(name, f"{h} must be an explicit boolean"))
        if ann.get("readOnlyHint") is True and ann.get("destructiveHint") is True:
            problems.append(Problem(name, "read-only and destructive at once"))
        if not (ann.get("title") or getattr(t, "title", None)):
            problems.append(Problem(name, "no title"))
        if len((t.description or "").strip()) < 20:
            problems.append(Problem(name, "description too short to tell the model what it does"))
        meta = t.meta or {}
        schemes = meta.get("securitySchemes")
        if not schemes or not all(s.get("type") in ("noauth", "oauth2") for s in schemes):
            problems.append(Problem(name, "securitySchemes missing or unknown"))
    return problems
