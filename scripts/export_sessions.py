#!/usr/bin/env python3
"""Copy this project's Claude Code transcripts into ai-sessions/, with keys redacted.

Run it LAST, just before sending — the session file keeps growing while you work.

    python scripts/export_sessions.py

It copies every .jsonl under ~/.claude/projects/<this-project>/ and replaces anything
that looks like an API key.  It then re-scans the output and refuses to finish if a
key-shaped string survived, so the check is not a matter of trusting the regex.
"""

from __future__ import annotations

import json
import re
import shutil
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
OUT = REPO / "ai-sessions"

# OpenAI (sk-..., sk-proj-...), Anthropic (sk-ant-...), AWS, generic bearer tokens
PATTERNS = [
    (re.compile(r"sk-(?:proj-|ant-|live-|test-)?[A-Za-z0-9_\-]{16,}"), "[REDACTED-API-KEY]"),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "[REDACTED-AWS-KEY]"),
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"), "[REDACTED-GITHUB-TOKEN]"),
    (re.compile(r"(?i)\b(bearer)\s+[A-Za-z0-9._\-]{24,}"), r"\1 [REDACTED-TOKEN]"),
]
# anything key-shaped that must not survive the pass
LEFTOVER = re.compile(r"sk-[A-Za-z0-9_\-]{16,}|AKIA[0-9A-Z]{16}|gh[pousr]_[A-Za-z0-9]{20,}")

# Material that is not mine to publish: private test data brought into the session
# from outside this package.  The span between the first and last entry mentioning
# any of these is removed whole, rather than line by line, so the transcript keeps
# a single clean gap instead of a scatter of holes mid-conversation, and a marker
# is written in its place.  Everything else in the log is untouched.
PRIVATE = (
    "downloads/holdout",
    "bayoucitypipe",
    "po 7781",
    "holdout",
    "handout",
)
MARKER = {
    "type": "redacted",
    "note": "A block of this session covered private test data belonging to the author "
            "and brought in from outside this package. It is removed here in full. "
            "Nothing else in this transcript is edited except API keys.",
}


def project_dir() -> Path:
    slug = str(REPO).replace("/", "-")
    return Path.home() / ".claude" / "projects" / slug


def main() -> int:
    src = project_dir()
    if not src.is_dir():
        print(f"no session directory at {src}", file=sys.stderr)
        print("if your tool cannot export, say so in AI_USAGE.md and bring the history to the interview.")
        return 1

    OUT.mkdir(parents=True, exist_ok=True)
    files = sorted(src.glob("*.jsonl"))
    if not files:
        print(f"no .jsonl transcripts in {src}", file=sys.stderr)
        return 1

    redactions = 0
    removed_entries = 0
    for f in files:
        lines = f.read_text(encoding="utf-8", errors="replace").splitlines()

        hits = [i for i, l in enumerate(lines) if any(p in l.lower() for p in PRIVATE)]
        if hits:
            lo, hi = hits[0], hits[-1]
            removed_entries += hi - lo + 1
            marker = dict(MARKER, entries_removed=hi - lo + 1)
            lines = lines[:lo] + [json.dumps(marker, ensure_ascii=False)] + lines[hi + 1:]

        text = "\n".join(lines) + "\n"
        for pat, repl in PATTERNS:
            text, n = pat.subn(repl, text)
            redactions += n
        dest = OUT / f.name
        dest.write_text(text, encoding="utf-8")
        print(f"  {f.name}  {len(text) / 1_000_000:.1f} MB")

    # verify, rather than assume, that nothing key-shaped or private survived
    survivors = []
    for dest in OUT.glob("*.jsonl"):
        for i, line in enumerate(dest.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
            low = line.lower()
            if any(p in low for p in PRIVATE) and '"type": "redacted"' not in low:
                survivors.append(f"{dest.name}:{i} (private material)")
    for dest in OUT.glob("*.jsonl"):
        for i, line in enumerate(dest.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
            if LEFTOVER.search(line):
                survivors.append(f"{dest.name}:{i}")

    print(f"\n{len(files)} transcript(s) -> {OUT}")
    print(f"{redactions} secret(s) redacted")
    if removed_entries:
        print(f"{removed_entries} entries removed as private material (one marker left in place)")

    if survivors:
        print("\nREFUSING TO FINISH — key-shaped strings survived redaction:", file=sys.stderr)
        for s in survivors[:20]:
            print(f"  {s}", file=sys.stderr)
        for dest in OUT.glob("*.jsonl"):
            dest.unlink()
        print("output deleted; fix the patterns and re-run.", file=sys.stderr)
        return 1

    print("verified: no key-shaped strings remain")
    print("\nalso check by hand:  grep -R 'sk-' ai-sessions/ | head")
    return 0


if __name__ == "__main__":
    sys.exit(main())
