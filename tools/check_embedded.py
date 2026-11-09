#!/usr/bin/env python3
"""Compile the module sources embedded inside userbot.py.

The userbot keeps `SYSTEM_CORE` and the `DEMO_*` modules as top-level string
constants.  `py_compile` only checks the outer source file, so this script
extracts those constants and compiles them as separate Python sources.

Usage:
    python tools/check_embedded.py userbot.py
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path


def main() -> int:
    path = Path(sys.argv[1] if len(sys.argv) > 1 else "userbot.py")
    if not path.is_file():
        print(f"not found: {path}", file=sys.stderr)
        return 1

    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except SyntaxError as exc:
        print(f"{path}: {exc}", file=sys.stderr)
        return 1

    sources: dict[str, str] = {}
    for node in tree.body:
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        target = node.targets[0]
        if not isinstance(target, ast.Name):
            continue
        if target.id != "SYSTEM_CORE" and not target.id.startswith("DEMO_"):
            continue
        try:
            value = ast.literal_eval(node.value)
        except Exception as exc:
            print(f"{target.id}: cannot evaluate: {exc}", file=sys.stderr)
            return 1
        if not isinstance(value, str):
            print(f"{target.id}: expected a string", file=sys.stderr)
            return 1
        sources[target.id] = value

    if not sources:
        print("no embedded module sources found", file=sys.stderr)
        return 1

    failed = False
    for name in sorted(sources):
        try:
            compile(sources[name], f"<{name}>", "exec")
        except SyntaxError as exc:
            failed = True
            print(f"{name}: {exc}", file=sys.stderr)
        else:
            print(f"{name}: OK")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
