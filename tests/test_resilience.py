#!/usr/bin/env python3
"""One bad email must never take down the run.

Builds a synthetic inbox of things that break readers — a truncated PDF, a
zero-byte attachment, an .xlsx that is really text, a message with no body, a
malformed Date, an attachment named with a path traversal — and asserts the
three invariants that make the output safe to act on:

  1. every .eml in the directory appears in parsed.json
  2. the run does not raise
  3. nothing unreadable comes back silently: it is flagged with a reason

Runs with or without OPENAI_API_KEY.  Without one it exercises the same paths
through the replay/degradation branch.

    python tests/test_resilience.py
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from email.message import EmailMessage
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from rfq.cli import _load_dotenv  # noqa: E402
from rfq.pipeline import run  # noqa: E402


def _email(subject: str, body: str, attachments=(), date="Mon, 05 Oct 2026 09:12:00 -0500") -> bytes:
    m = EmailMessage()
    m["From"] = "Pat Buyer <pat@example-customer.example>"
    m["To"] = "rfq@ridgeline-supply.example"
    m["Subject"] = subject
    if date is not None:
        m["Date"] = date
    m["Message-ID"] = f"<{abs(hash(subject))}@example-customer.example>"
    m.set_content(body)
    for name, mime, data in attachments:
        maintype, _, subtype = mime.partition("/")
        m.add_attachment(data, maintype=maintype, subtype=subtype, filename=name)
    return m.as_bytes()


CASES: dict[str, bytes] = {
    # a PDF header with nothing valid behind it
    "B001": _email(
        "Quote request - tie in materials",
        "Please quote the attached list, we need it Friday.",
        [("materials.pdf", "application/pdf", b"%PDF-1.4\n" + b"\x00\xff" * 400)],
    ),
    # an attachment that arrived empty
    "B002": _email(
        "RFQ 88-12 flanges",
        "RFQ attached.",
        [("RFQ_88-12.xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", b"")],
    ),
    # declared .xlsx, actually CSV text — routing must go by content
    "B003": _email(
        "material list",
        "List attached, please price.",
        [(
            "list.xlsx",
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            b'Qty,UOM,Size,Description\n12,EA,2",\"90 elbow, SW, 3000#, A105\"\n',
        )],
    ),
    # a zip that is neither xlsx nor docx
    "B004": _email(
        "drawings",
        "See attached.",
        [("pack.zip", "application/zip", b"PK\x03\x04" + b"\x00" * 200)],
    ),
    # an image that will not decode
    "B005": _email(
        "list from the yard",
        "Here's the list, need prices today.",
        [("IMG_9001.jpg", "image/jpeg", b"\xff\xd8\xff" + b"\x11" * 300)],
    ),
    # no body at all, and a nasty filename
    "B006": _email(
        "",
        "",
        [("../../etc/passwd", "application/pdf", b"%PDF-1.4\nbroken")],
    ),
    # unparseable Date header: the inbox still has to sort and link
    "B007": _email("Re: quote", "Any update on this one?", date="not-a-date"),
    # a legacy binary Office file we have no reader for
    "B008": _email(
        "BOM",
        "BOM attached, pricing please.",
        [("bom.xls", "application/vnd.ms-excel", b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 300)],
    ),
}


def main() -> int:
    _load_dotenv()
    tmp = Path(tempfile.mkdtemp(prefix="rfq-resilience-"))
    try:
        inbox = tmp / "emails"
        inbox.mkdir()
        for eid, raw in CASES.items():
            (inbox / f"{eid}.eml").write_bytes(raw)

        parsed, cost, results = run(
            inbox,
            cache_dir=REPO / "cache",
            api_key=os.getenv("OPENAI_API_KEY"),
            concurrency=4,
        )

        failures: list[str] = []

        # 1 — nothing disappears
        for eid in CASES:
            if eid not in parsed:
                failures.append(f"{eid}: missing from parsed.json")

        # 2 — the shape is still valid
        for eid, row in parsed.items():
            if not isinstance(row.get("line_items"), list):
                failures.append(f"{eid}: line_items is not a list")
            if not isinstance(row.get("needs_review"), bool):
                failures.append(f"{eid}: needs_review is not a bool")
            if row["needs_review"] and not (row.get("review_reason") or "").strip():
                failures.append(f"{eid}: flagged for review with no reason")

        # 3 — nothing unreadable comes back silently
        for eid in ("B001", "B002", "B004", "B005", "B006", "B008"):
            row = parsed.get(eid, {})
            if not row.get("line_items") and not row.get("needs_review"):
                failures.append(f"{eid}: unreadable attachment returned nothing and no flag (silent loss)")

        # 4 — content routing beat the wrong extension
        b3 = parsed.get("B003", {})
        if not b3.get("line_items") and not b3.get("needs_review"):
            failures.append("B003: CSV mislabelled .xlsx was neither read nor flagged")

        for eid in sorted(parsed):
            row = parsed[eid]
            mark = "flag" if row["needs_review"] else "ok  "
            print(f"  {eid} {mark} {row['classification']:<15} {len(row['line_items'])} lines  {(row.get('review_reason') or '')[:70]}")
        print(f"\n{len(parsed)} emails, {cost['model_calls']} model calls, ${cost['cost_usd']:.4f}")

        if failures:
            print("\nFAILURES:")
            for f in failures:
                print(f"  - {f}")
            return 1
        print("\nall resilience invariants hold")
        return 0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
