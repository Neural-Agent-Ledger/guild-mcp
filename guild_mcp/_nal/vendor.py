"""Copy this package into a consumer, byte for byte.

    python -m nal_mcp_sdk.hosted.vendor <consumer>/<pkg>/_nal

Public servers (guild-mcp) cannot install this private repo, so they carry a
copy. ``VENDORED.json`` beside the copy records each file's sha256; the
consumer's test ``drift(...)`` fails when its copy and the record disagree
(someone edited the copy) or, with the SDK checked out next to it, when the
SDK moved on and the copy was not refreshed.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
# vendor.py travels too: the copy checks itself with drift().
FILES = ("__init__.py", "oauth.py", "tools.py", "apps.py", "spotlight.py", "plugin.py", "vendor.py")
RECORD = "VENDORED.json"


def _sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def vendor(target: Path) -> dict[str, str]:
    target.mkdir(parents=True, exist_ok=True)
    record = {}
    for name in FILES:
        shutil.copyfile(HERE / name, target / name)
        record[name] = _sha(target / name)
    (target / RECORD).write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    return record


def drift(copy: Path, sdk: Path | None = None) -> list[str]:
    """What is wrong with a vendored copy; empty when it is faithful."""
    record = json.loads((copy / RECORD).read_text(encoding="utf-8"))
    problems = []
    if sorted(record) != sorted(FILES):
        problems.append(f"{RECORD} lists {sorted(record)}, the SDK ships {sorted(FILES)}")
    for name, sha in record.items():
        f = copy / name
        if not f.is_file():
            problems.append(f"{name}: missing")
        elif _sha(f) != sha:
            problems.append(f"{name}: edited in place; change nal-mcp-sdk and re-vendor")
        if sdk is not None and (sdk / name).is_file() and _sha(sdk / name) != sha:
            problems.append(f"{name}: nal-mcp-sdk has a newer version; re-vendor")
    return problems


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print(__doc__)
        sys.exit(2)
    for name, sha in vendor(Path(sys.argv[1])).items():
        print(sha[:12], name)
