#!/usr/bin/env python3
"""Verify the loader pin and basic source layout.

Usage:
    python tools/check_loader.py miniub-0.5.0.plugin userbot.py
"""

from __future__ import annotations

import ast
import hashlib
import sys
from pathlib import Path


def top_value(path: Path, name: str):
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in tree.body:
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        target = node.targets[0]
        if isinstance(target, ast.Name) and target.id == name:
            return ast.literal_eval(node.value)
    raise SystemExit(f"{name} not found in {path}")


def main() -> int:
    loader = Path(sys.argv[1] if len(sys.argv) > 1 else "miniub-0.5.0.plugin")
    userbot = Path(sys.argv[2] if len(sys.argv) > 2 else "userbot.py")

    expected = str(top_value(loader, "USERBOT_SHA256")).strip().lower()
    url = str(top_value(loader, "USERBOT_URL")).strip()
    actual = hashlib.sha256(userbot.read_bytes()).hexdigest()

    if not url.startswith("https://"):
        print(f"USERBOT_URL must use https: {url}", file=sys.stderr)
        return 1
    if not url.endswith("/userbot.py"):
        print(f"USERBOT_URL must point to userbot.py: {url}", file=sys.stderr)
        return 1
    if len(expected) != 64 or any(c not in "0123456789abcdef" for c in expected):
        print("USERBOT_SHA256 must be a 64-character hex digest", file=sys.stderr)
        return 1
    if expected != actual:
        print(f"SHA256 mismatch: loader={expected}, userbot={actual}", file=sys.stderr)
        return 1

    print(f"loader pin matches {userbot} ({actual})")
    print(f"userbot URL: {url}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
