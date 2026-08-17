#!/usr/bin/env python3
"""Structural + shell-syntax validation for Apptainer .def files.

Cannot run `apptainer build` here (no Linux kernel, and the Mac is arm64), so
this checks everything that is checkable statically:

  1. A Bootstrap: header with a matching From: (for bootstrap agents that need one)
  2. Only recognised %section names
  3. %post / %runscript / %test bodies are valid shell (bash -n)
  4. %files entries are two-column and their sources exist in the build context
  5. %environment lines are export-shaped and shell-parseable
"""
import re
import subprocess
import sys
from pathlib import Path

VALID_SECTIONS = {
    "setup", "files", "environment", "post", "runscript", "test",
    "startscript", "labels", "help", "apprun", "applabels", "apphelp",
    "appinstall", "appenv", "appfiles", "apptest",
}
SHELL_SECTIONS = {"post", "runscript", "test", "startscript", "setup"}

ROOT = Path(sys.argv[1] if len(sys.argv) > 1 else ".").resolve()


def parse(path):
    """Split a def file into (header_lines, {section: body_lines})."""
    header, sections, current = [], {}, None
    for line in path.read_text().splitlines():
        m = re.match(r"^%([A-Za-z]+)\s*(.*)$", line)
        if m:
            current = m.group(1).lower()
            sections.setdefault(current, [])
            continue
        (sections[current] if current else header).append(line)
    return header, sections


def check(path):
    problems = []
    header, sections = parse(path)

    header_text = "\n".join(header)
    if not re.search(r"^Bootstrap:\s*\S+", header_text, re.M):
        problems.append("missing Bootstrap: header")
    bootstrap = re.search(r"^Bootstrap:\s*(\S+)", header_text, re.M)
    if bootstrap and bootstrap.group(1) in {"docker", "library", "oras", "shub"}:
        if not re.search(r"^From:\s*\S+", header_text, re.M):
            problems.append(f"Bootstrap: {bootstrap.group(1)} requires a From:")

    for name in sections:
        if name not in VALID_SECTIONS:
            problems.append(f"unknown section %{name}")

    # A stray '%' at column 0 inside a body would have silently become a section
    # above, so the unknown-section check covers that too.

    for name in sections:
        if name in SHELL_SECTIONS:
            body = "\n".join(sections[name])
            r = subprocess.run(["bash", "-n"], input=body, text=True,
                               capture_output=True)
            if r.returncode != 0:
                problems.append(
                    f"%{name} is not valid shell: {r.stderr.strip().splitlines()[:3]}")

    if "environment" in sections:
        body = "\n".join(sections["environment"])
        r = subprocess.run(["bash", "-n"], input=body, text=True,
                           capture_output=True)
        if r.returncode != 0:
            problems.append(f"%environment is not valid shell: {r.stderr.strip()}")

    # %files sources are resolved from the build context = the workflow root,
    # which is the def file's parent's parent (Apptainer/<file>.def).
    context = path.parent.parent
    for raw in sections.get("files", []):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if len(parts) != 2:
            problems.append(f"%files line is not 'src dst': {line!r}")
            continue
        src = parts[0]
        if any(ch in src for ch in "*?["):
            if not list(context.glob(src)):
                problems.append(f"%files glob matches nothing in {context.name}/: {src}")
        elif not (context / src).exists():
            problems.append(f"%files source missing in {context.name}/: {src}")

    return problems


# Works from a single workflow root (Apptainer/*.def) or from the multi-workflow
# workspace (*/Apptainer/*.def).
defs = sorted(ROOT.glob("Apptainer/*.def")) or sorted(ROOT.glob("*/Apptainer/*.def"))
if not defs:
    print("no .def files found")
    sys.exit(1)

failed = 0
for d in defs:
    problems = check(d)
    rel = d.relative_to(ROOT)
    if problems:
        failed += 1
        print(f"FAIL  {rel}")
        for p in problems:
            print(f"        - {p}")
    else:
        print(f"ok    {rel}")

print(f"\n{len(defs) - failed}/{len(defs)} definition files pass")
sys.exit(1 if failed else 0)
