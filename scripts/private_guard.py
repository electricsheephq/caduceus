#!/usr/bin/env python3
"""Refuse commits that could carry real personal data (a local developer tool; not part of the plugin).

Checks staged files (or a commit message) for:
- entries of a private denylist kept OUTSIDE the repo: ``$CADUCEUS_DENYLIST`` or ``~/.config/caduceus/denylist.txt``.
  One entry per line; ``sha256:<hex>`` lines match whole files, other lines match case-insensitive whole words;
- files that look like real inputs: raw DNA data (many genotype rows), a Promethease report (many encoded
  chunks), an Apple Health export, or a FHIR bundle with patient identifiers;
- files over 1 MB, and binary blobs outside docs/media.

Install the hooks once per clone:  python3 scripts/private_guard.py --install
It prints which file and which check failed, never the matching text.
"""
from __future__ import annotations

import hashlib
import os
import re
import subprocess
import sys
from pathlib import Path

MAX_BYTES = 1 << 20
MEDIA_OK = ("docs/media/",)
_GENOTYPE_ROW = re.compile(rb"^(?:rs|i)\d+\t[0-9XYMT]{1,2}\t\d+\t[ACGTDI-]{1,2}\r?$", re.M)
_PROM_CHUNK = re.compile(rb"decompressString\('")


def _denylist() -> tuple[set[str], list[re.Pattern]]:
    path = Path(os.environ.get("CADUCEUS_DENYLIST") or Path.home() / ".config" / "caduceus" / "denylist.txt")
    hashes, words = set(), []
    if path.is_file():
        for line in path.read_text(encoding="utf-8").splitlines():
            entry = line.strip()
            if not entry or entry.startswith("#"):
                continue
            if entry.lower().startswith("sha256:"):
                hashes.add(entry[7:].strip().lower())
            else:
                words.append(re.compile(r"(?<!\w)" + re.escape(entry).replace(r"\ ", r"[\s_-]+") + r"(?!\w)", re.I))
    return hashes, words


def _staged() -> list[tuple[str, bytes]]:
    names = subprocess.run(["git", "diff", "--cached", "--name-only", "--diff-filter=ACMR", "-z"],
                           check=True, capture_output=True).stdout.split(b"\0")
    out = []
    for raw in names:
        if raw:
            name = raw.decode("utf-8", "replace")
            out.append((name, subprocess.run(["git", "show", f":{name}"], check=True, capture_output=True).stdout))
    return out


def check(files, hashes, words) -> list[str]:
    problems = []
    for name, data in files:
        if hashlib.sha256(data).hexdigest() in hashes:
            problems.append(f"{name}: is a copy of a private file")
            continue
        if len(data) > MAX_BYTES:
            problems.append(f"{name}: larger than 1 MB")
        if b"\0" in data[:8192] and not name.startswith(MEDIA_OK):
            problems.append(f"{name}: binary file outside docs/media")
        if len(_GENOTYPE_ROW.findall(data)) > 500:
            problems.append(f"{name}: looks like real DNA raw data (generate fixtures at test time instead)")
        if len(_PROM_CHUNK.findall(data)) > 50:
            problems.append(f"{name}: looks like a real Promethease report")
        if b"<HealthData" in data and data.count(b"<Record ") > 200:
            problems.append(f"{name}: looks like a real Apple Health export")
        text = data.decode("utf-8", "replace")
        if any(w.search(text) for w in words):
            problems.append(f"{name}: contains an entry from your private denylist")
    return problems


def install() -> int:
    hooks = Path(subprocess.run(["git", "rev-parse", "--git-common-dir"], check=True, capture_output=True,
                                text=True).stdout.strip()) / "hooks"
    hooks.mkdir(parents=True, exist_ok=True)
    script = Path(__file__).resolve()
    for hook, arg in (("pre-commit", ""), ("commit-msg", ' --message "$1"')):
        target = hooks / hook
        target.write_text(f'#!/bin/sh\nexec python3 -I "{script}"{arg}\n', encoding="utf-8")
        target.chmod(0o755)
    print(f"installed pre-commit and commit-msg hooks in {hooks}")
    return 0


def main(argv) -> int:
    if "--install" in argv:
        return install()
    hashes, words = _denylist()
    if "--message" in argv:
        message = Path(argv[argv.index("--message") + 1]).read_bytes()
        problems = check([("commit message", message)], set(), words)
    else:
        problems = check(_staged(), hashes, words)
    if problems:
        print("private_guard: commit refused", file=sys.stderr)
        for problem in problems:
            print("  - " + problem, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
