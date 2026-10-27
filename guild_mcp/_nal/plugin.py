"""Check and zip a plugin package for the OpenAI plugin directory (ChatGPT and
Codex share it).

A package is a folder with, at its root::

    plugin.json        manifest (agent-plugins.org schema + extensions.com.openai)
    mcp.json           the remote MCP server(s): streamable-http, https
    skills/<name>/SKILL.md
    assets/            icons, screenshots

Usage::

    python -m nal_mcp_sdk.hosted.plugin check plugin/ [--version X.Y.Z] [--skills skills/]
    python -m nal_mcp_sdk.hosted.plugin build plugin/ dist/ [--version X.Y.Z] [--skills skills/]

``--skills`` takes the skills from another folder (the repo's own ``skills/``,
which the Claude Code plugin ships too) and packs them as ``skills/``.

``check`` exits 1 and names every problem; ``build`` checks first and writes
``dist/<name>-<version>.zip``.
"""

from __future__ import annotations

import json
import re
import sys
import zipfile
from pathlib import Path

_KEBAB = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_SEMVER = re.compile(r"^\d+\.\d+\.\d+$")
# What the directory listing shows; the review rejects a listing without them.
_INTERFACE_REQUIRED = (
    "displayName",
    "shortDescription",
    "longDescription",
    "developerName",
    "category",
    "websiteURL",
    "privacyPolicyURL",
    "termsOfServiceURL",
)
_ICON_MAX_BYTES = 5 * 1024


def _front_matter(text: str) -> dict[str, str] | None:
    m = re.match(r"^---\r?\n(.*?)\r?\n---\r?\n", text, re.S)
    if not m:
        return None
    out: dict[str, str] = {}
    for line in m.group(1).splitlines():
        if ":" in line and not line.startswith((" ", "\t")):
            k, v = line.split(":", 1)
            out[k.strip()] = v.strip()
    return out


def check(root: Path, *, version: str | None = None, skills: Path | None = None) -> list[str]:
    """Every problem with the package at ``root``; empty means ready to build."""
    problems: list[str] = []
    manifest_path = root / "plugin.json"
    if not manifest_path.is_file():
        return [f"{manifest_path}: missing"]
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        return [f"plugin.json: not JSON ({e})"]

    name = manifest.get("name", "")
    if not _KEBAB.fullmatch(str(name)):
        problems.append(f"plugin.json: name {name!r} is not kebab-case")
    v = manifest.get("version", "")
    if not _SEMVER.fullmatch(str(v)):
        problems.append(f"plugin.json: version {v!r} is not X.Y.Z")
    if version is not None and v != version:
        problems.append(f"plugin.json: version {v} differs from the server's {version}")
    if not manifest.get("description"):
        problems.append("plugin.json: no description")
    if not (manifest.get("author") or {}).get("name"):
        problems.append("plugin.json: no author.name")

    ext = (manifest.get("extensions") or {}).get("com.openai") or {}
    interface = ext.get("interface") or {}
    for key in _INTERFACE_REQUIRED:
        if not interface.get(key):
            problems.append(f"plugin.json: extensions.com.openai.interface.{key} is empty")
    for key in ("websiteURL", "privacyPolicyURL", "termsOfServiceURL"):
        url = interface.get(key)
        if url and not str(url).startswith("https://"):
            problems.append(f"plugin.json: {key} must be https")
    for key in ("logo", "composerIcon"):
        problems += _check_path(root, interface.get(key), f"interface.{key}", required=True)
    icon = interface.get("composerIcon")
    if icon and (root / icon).is_file() and (root / icon).stat().st_size > _ICON_MAX_BYTES:
        problems.append(f"plugin.json: {icon} is over 5 KB")
    for shot in interface.get("screenshots") or []:
        problems += _check_path(root, shot, "interface.screenshots", required=True)
    onboarding = ext.get("onboardingSkill")
    if onboarding and skills is not None and str(onboarding).startswith("./skills/"):
        # Packed from the other folder: look for it there.
        if not (skills / str(onboarding).removeprefix("./skills/")).is_file():
            problems.append(f"plugin.json: onboardingSkill {onboarding!r} is not in {skills}")
    elif onboarding:
        problems += _check_path(root, onboarding, "onboardingSkill", required=True)

    problems += _check_mcp(root)
    problems += _check_skills(skills or root / "skills")
    return problems


def _check_path(root: Path, value: object, label: str, *, required: bool) -> list[str]:
    if not value:
        return [f"plugin.json: {label} is empty"] if required else []
    s = str(value)
    if not s.startswith("./"):
        return [f"plugin.json: {label} {s!r} must start with ./"]
    p = (root / s).resolve()
    if root.resolve() not in p.parents:
        return [f"plugin.json: {label} {s!r} leaves the plugin root"]
    if not p.is_file():
        return [f"plugin.json: {label} {s!r} does not exist"]
    return []


def _check_mcp(root: Path) -> list[str]:
    path = root / "mcp.json"
    if not path.is_file():
        return []  # a skills-only plugin
    try:
        servers = json.loads(path.read_text(encoding="utf-8")).get("mcpServers") or {}
    except json.JSONDecodeError as e:
        return [f"mcp.json: not JSON ({e})"]
    if not servers:
        return ["mcp.json: no mcpServers"]
    problems = []
    for key, s in servers.items():
        if s.get("type") != "streamable-http":
            problems.append(f"mcp.json: {key} type must be streamable-http")
        if not str(s.get("url", "")).startswith("https://"):
            problems.append(f"mcp.json: {key} url must be a public https URL")
        auth = ((s.get("extensions") or {}).get("com.openai") or {}).get("auth") or {}
        if auth.get("type") not in ("none", "oauth", "mixed", "api_key"):
            problems.append(f"mcp.json: {key} extensions.com.openai.auth.type is unset")
        if any(k in json.dumps(s).lower() for k in ("secret", "password")):
            problems.append(f"mcp.json: {key} names a secret; the package declares settings only")
    return problems


def _check_skills(skills: Path) -> list[str]:
    if not skills.is_dir():
        return []
    problems = []
    for d in sorted(p for p in skills.iterdir() if p.is_dir()):
        f = d / "SKILL.md"
        if not f.is_file():
            problems.append(f"skills/{d.name}: no SKILL.md")
            continue
        fm = _front_matter(f.read_text(encoding="utf-8"))
        if fm is None:
            problems.append(f"skills/{d.name}/SKILL.md: no front matter")
            continue
        if fm.get("name") != d.name:
            problems.append(f"skills/{d.name}/SKILL.md: name {fm.get('name')!r} != folder")
        desc = fm.get("description", "")
        if not desc:
            problems.append(f"skills/{d.name}/SKILL.md: no description")
        elif len(desc) > 1024:
            problems.append(f"skills/{d.name}/SKILL.md: description over 1024 characters")
    return problems


def build(
    root: Path, out_dir: Path, *, version: str | None = None, skills: Path | None = None
) -> Path:
    problems = check(root, version=version, skills=skills)
    if problems:
        raise ValueError("\n".join(problems))
    manifest = json.loads((root / "plugin.json").read_text(encoding="utf-8"))
    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / f"{manifest['name']}-{manifest['version']}.zip"
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as z:
        for p in sorted(root.rglob("*")):
            if p.is_file() and "__pycache__" not in p.parts:
                z.write(p, p.relative_to(root).as_posix())
        if skills is not None:
            for p in sorted(skills.rglob("*")):
                if p.is_file() and "__pycache__" not in p.parts:
                    z.write(p, "skills/" + p.relative_to(skills).as_posix())
    return target


def main(argv: list[str]) -> int:
    if len(argv) < 2 or argv[0] not in ("check", "build"):
        print(__doc__)
        return 2
    version = argv[argv.index("--version") + 1] if "--version" in argv else None
    skills = Path(argv[argv.index("--skills") + 1]) if "--skills" in argv else None
    root = Path(argv[1])
    if argv[0] == "check":
        problems = check(root, version=version, skills=skills)
        for p in problems:
            print(p)
        print("ok" if not problems else f"{len(problems)} problem(s)")
        return 1 if problems else 0
    try:
        print(build(root, Path(argv[2]), version=version, skills=skills))
    except ValueError as e:
        print(e)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
