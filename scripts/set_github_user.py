"""Replace the GitHub username placeholder in the links of this repository.

    python scripts/set_github_user.py NEWNAME

Rewrites ``fakhter-uvm`` -> NEWNAME in README.md, pyproject.toml,
CONTRIBUTING.md and docs/**/*.html|*.md|*.js, and prints every file changed.

Author: Farzana Akhter
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PLACEHOLDER = "fakhter-uvm"


def targets():
    for name in ("README.md", "pyproject.toml", "CONTRIBUTING.md"):
        path = ROOT / name
        if path.exists():
            yield path
    for pattern in ("*.html", "*.md", "*.js"):
        yield from sorted((ROOT / "docs").rglob(pattern))


def main(argv) -> int:
    if len(argv) != 1 or not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})", argv[0]):
        print("usage: python scripts/set_github_user.py NEWNAME   (a valid GitHub user or organization name)")
        return 2
    new = argv[0]
    changed = 0
    for path in targets():
        text = path.read_text(encoding="utf-8")
        if PLACEHOLDER in text:
            path.write_text(text.replace(PLACEHOLDER, new), encoding="utf-8")
            print(f"updated {path.relative_to(ROOT)} ({text.count(PLACEHOLDER)} link(s))")
            changed += 1
    print(f"{changed} file(s) changed" if changed else f"nothing to change: '{PLACEHOLDER}' not found")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
