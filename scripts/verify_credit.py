#!/usr/bin/env python3
"""Fail closed when FrameGrab attribution or release privacy regresses."""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CREDIT = "By TheVibeFounder"
COPYRIGHT = "Copyright (c) 2026 TheVibeFounder"
REQUIRED_TEXT = {
    "ACKNOWLEDGMENTS.md": [CREDIT, "Original reclip license (MIT)", "Third-party components"],
    "AGENTS.md": [CREDIT, "NOTICE.md", "python3 scripts/verify_credit.py"],
    "CLAUDE.md": [CREDIT, "NOTICE.md", "python3 scripts/verify_credit.py"],
    "LICENSE": [COPYRIGHT, "MIT License", "Permission is hereby granted"],
    "NOTICE.md": [CREDIT, COPYRIGHT, "must not be removed"],
    "README.md": [CREDIT, COPYRIGHT, "NOTICE.md"],
    "app.py": [COPYRIGHT],
    "framegrab_core/__init__.py": [COPYRIGHT],
    "framegrab_core/storage.py": [COPYRIGHT],
    "framegrab_core/artifacts.py": [COPYRIGHT],
    "framegrab_core/library.py": [COPYRIGHT],
    "framegrab_core/runtime.py": [COPYRIGHT],
    "static/framegrab.css": [COPYRIGHT],
    "static/framegrab.js": [COPYRIGHT],
    "static/workspace.js": [COPYRIGHT],

    "templates/index.html": [COPYRIGHT],
    "framegrab.sh": [COPYRIGHT],
}
SKIP_PARTS = {
    ".git",
    ".pytest_cache",
    ".transcribe-venv",
    ".venv",
    "Downloads",
    "__pycache__",
    "downloads",
    "private-projects",
    "research-projects",
    "venv",
}
MAC_USER_ROOT = "/".join(["", "Users", ""])
WINDOWS_USER_ROOT = "C:" + "\\" + "Users" + "\\"
FORBIDDEN = {
    "personal home path": re.compile(
        rf"(?:{re.escape(MAC_USER_ROOT)}|[A-Z]:{re.escape(WINDOWS_USER_ROOT[2:])})"
    ),
    "email address": re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.I),
    "private key": re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    "credential-shaped token": re.compile(r"\b(?:sk-[A-Za-z0-9_-]{20,}|ghp_[A-Za-z0-9]{20,}|AIza[A-Za-z0-9_-]{20,})\b"),
    "personal legal name": re.compile("Ajay" + r"\s+" + "Yadav", re.I),
    "internal brand source path": re.compile("Brand" + r"\s+Guide\s+&\s+Assets/", re.I),
}


def release_files() -> list[Path]:
    if (ROOT / ".git").exists():
        output = subprocess.check_output(
            ["git", "ls-files", "-z"], cwd=ROOT
        ).decode("utf-8")
        return [ROOT / item for item in output.split("\0") if item]
    return [
        file
        for file in ROOT.rglob("*")
        if file.is_file() and not any(part in SKIP_PARTS for part in file.relative_to(ROOT).parts)
    ]


def read_text(file: Path) -> str | None:
    data = file.read_bytes()
    if b"\x00" in data:
        return None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None


def verify() -> list[str]:
    errors: list[str] = []
    for relative, markers in REQUIRED_TEXT.items():
        file = ROOT / relative
        if not file.is_file():
            errors.append(f"required attribution file is missing: {relative}")
            continue
        text = file.read_text(encoding="utf-8")
        for marker in markers:
            if marker not in text:
                errors.append(f"{relative}: protected attribution is missing: {marker}")

    for file in release_files():
        relative = file.relative_to(ROOT).as_posix()
        text = read_text(file)
        if text is None:
            continue
        for label, pattern in FORBIDDEN.items():
            if pattern.search(text):
                errors.append(f"{relative}: contains {label}")
    return errors


def main() -> int:
    errors = verify()
    if errors:
        for error in errors:
            print(f"FAIL: {error}", file=sys.stderr)
        return 1
    print("PASS: FrameGrab attribution is intact and distributable source contains no personal identity, email, user path, or credential pattern.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
