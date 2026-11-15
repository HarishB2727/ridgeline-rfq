#!/usr/bin/env python3
"""Input shapes the dev inbox does not contain.

The dev set has 24 emails and the held-out inbox has ~40 "from the same company,
the same kinds of surprises, none of the same strings".  The cheapest way to find
real defects is not to re-read those 24 — it is to ask what shape of input they
happen not to include, and build it.

Every case here is something a real RFQ inbox produces weekly.  Run it and read
the findings; it reports rather than merely passes, because some of these have no
single right answer and the interesting part is what the system actually does.

    python tests/test_shapes.py
"""

from __future__ import annotations

import io
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

CSV_BOM = (
    b'Qty,UOM,Size,Description\n'
    b'12,EA,"1-1/2""","90 elbow, SW, 3000#, A105"\n'
    b'6,EA,"1-1/2""","Tee, SW, 3000#, A105"\n'
    b'8,EA,"1-1/2""","Union, SW, 3000#, A105"\n'
)


def msg(frm, subject, body, *, date, mid, attachments=(), in_reply_to=None, headers=(), charset=None):
    m = EmailMessage()
    m["From"] = frm
    m["To"] = "rfq@ridgeline-supply.example"
    m["Subject"] = subject
    m["Date"] = date
    m["Message-ID"] = mid
    if in_reply_to:
        m["In-Reply-To"] = in_reply_to
        m["References"] = in_reply_to
    for k, v in headers:
        m[k] = v
    if charset:
        m.set_content(body, charset=charset)
    else:
        m.set_content(body)
    for name, mime, data in attachments:
        maintype, _, subtype = mime.partition("/")
        m.add_attachment(data, maintype=maintype, subtype=subtype, filename=name)
    return m


def build(inbox: Path) -> None:
    # S001 — our rep forwards a customer email; the BOM is attached to the INNER message
    inner = msg(
        "Priya Raman <priya@hollismech.example>",
        "material list - condensate return",
        "Marcus,\n\nList attached for job HM-7716. Please price it.\n\nPriya",
        date="Mon, 02 Nov 2026 08:02:00 -0600",
        mid="<inner-1@hollismech.example>",
        attachments=[("HM-7716_list.csv", "text/csv", CSV_BOM)],
    )
    outer = msg(
        "Marcus Doyle <marcus.doyle@ridgeline-supply.example>",
        "FW: material list - condensate return",
        "RFQ desk - please quote this for Priya, she needs it Friday.\n\nMarcus",
        date="Mon, 02 Nov 2026 09:00:00 -0600",
        mid="<s001@ridgeline-supply.example>",
        attachments=[("forwarded.eml", "message/rfc822", inner.as_bytes())],
    )
    (inbox / "S001.eml").write_bytes(outer.as_bytes())

    # S002 — two attachments with the SAME filename and different contents
    second = CSV_BOM.replace(b"12,EA", b"40,EA").replace(b"90 elbow", b"45 elbow")
    (inbox / "S002.eml").write_bytes(
        msg(
            "Dale Thibodeaux <dale@pelicanbay.example>",
            "two areas - please quote both sheets",
            "Both sheets attached, area A and area B. Price them separately.",
            date="Mon, 02 Nov 2026 10:00:00 -0600",
            mid="<s002@pelicanbay.example>",
            attachments=[("sheet.csv", "text/csv", CSV_BOM), ("sheet.csv", "text/csv", second)],
        ).as_bytes()
    )

    # S003 — windows-1252 body (an em dash and a degree sign), as Outlook sends
    body = "Hi,\n\nPlease quote — urgent:\n\n6 ea – 2\" 90° elbow, SW 3000# A105\n4 ea – 2\" tee, SW 3000# A105\n\nRegards,\nJosé"
    (inbox / "S003.eml").write_bytes(
        msg(
            "Jose Alvarez <jose@gulfmech.example>",
            "urgent - 2in socket weld fittings",
            body,
            date="Mon, 02 Nov 2026 11:00:00 -0600",
            mid="<s003@gulfmech.example>",
            charset="windows-1252",
        ).as_bytes()
    )

    # S004 — out-of-office.  The brief names it; dev only has a bounce.
    (inbox / "S004.eml").write_bytes(
        msg(
            "Gwen Aldridge <galdridge@ironwoodskids.example>",
            "Automatic reply: RFQ - Skid 14 piping package",
            "I am out of the office until 10 November with limited access to email.\n"
            "For urgent purchasing matters contact Dana Reyes.\n\nGwen",
            date="Mon, 02 Nov 2026 12:00:00 -0600",
            mid="<s004@ironwoodskids.example>",
            headers=[("Auto-Submitted", "auto-replied"), ("X-Auto-Response-Suppress", "All")],
        ).as_bytes()
    )

    # S005/S006/S007 — a three-step thread: request, revision, revision again
    rfq = msg(
        "Tomas Reyes <treyes@southfork.example>",
        "Tank farm phase 3 - pricing",
        "Hi,\n\nPlease quote:\n\n1) 20 jts - 2\" SCH 40 A53B ERW pipe, T&C\n"
        "2) 10 ea - 2\" 150# malleable iron 90 ell, black\n"
        "3) 6 ea - 2\" 150# malleable iron tee, black\n\nTomas",
        date="Tue, 03 Nov 2026 08:00:00 -0600",
        mid="<s005@southfork.example>",
    )
    (inbox / "S005.eml").write_bytes(rfq.as_bytes())
    (inbox / "S006.eml").write_bytes(
        msg(
            "Tomas Reyes <treyes@southfork.example>",
            "Re: Tank farm phase 3 - pricing",
            "Change line 1 to 26 jts. Everything else stays.\n\nTomas",
            date="Tue, 03 Nov 2026 10:00:00 -0600",
            mid="<s006@southfork.example>",
            in_reply_to="<s005@southfork.example>",
        ).as_bytes()
    )
    (inbox / "S007.eml").write_bytes(
        msg(
            "Tomas Reyes <treyes@southfork.example>",
            "Re: Tank farm phase 3 - pricing",
            "Sorry - also drop line 3, and add 4 ea - 2\" 150# malleable iron union, black.\n\nTomas",
            date="Tue, 03 Nov 2026 14:00:00 -0600",
            mid="<s007@southfork.example>",
            in_reply_to="<s006@southfork.example>",
        ).as_bytes()
    )

    # S008/S009 — same sender, same generic subject, DIFFERENT orders.
    # These must NOT be collapsed into a duplicate.
    (inbox / "S008.eml").write_bytes(
        msg(
            "Cody Landry <cody@landryoil.example>",
            "pricing request",
            "Need pricing:\n\n10 ea - 3\" 150# RF weld neck flange, A105\n",
            date="Wed, 04 Nov 2026 09:00:00 -0600",
            mid="<s008@landryoil.example>",
        ).as_bytes()
    )
    (inbox / "S009.eml").write_bytes(
        msg(
            "Cody Landry <cody@landryoil.example>",
            "pricing request",
            "Need pricing:\n\n25 ea - 6\" 300# RF blind flange, A105\n",
            date="Wed, 04 Nov 2026 15:00:00 -0600",
            mid="<s009@landryoil.example>",
        ).as_bytes()
    )

    # S010 — units and numbers the dev set never exercises
    (inbox / "S010.eml").write_bytes(
        msg(
            "Ben Szymanski <ben@oakhollow.example>",
            "consumables for the glycol line",
            "Hi,\n\nPlease quote:\n\n"
            "twelve lengths of 1\" SCH 40 galvanized pipe, TBE\n"
            "1,200 ft of 3/4\" SCH 40 galvanized pipe, TBE\n"
            "3 gal - thread sealant\n"
            "2 bags - pipe joint compound\n"
            "1 case - shop rags\n"
            "4 pair - welding gloves\n"
            "2 dozen - 1/2\" hex nuts, zinc\n"
            "2.5 ft of 2\" pipe insulation\n\nBen",
            date="Thu, 05 Nov 2026 09:00:00 -0600",
            mid="<s010@oakhollow.example>",
        ).as_bytes()
    )


def _table_png(rows, *, header, note=None, numbered=True) -> bytes:
    """A plain rendered table, tall enough that it will be read as strips."""
    from PIL import Image, ImageDraw

    w, row_h, top = 1150, 52, 150
    img = Image.new("RGB", (w, top + row_h * (len(rows) + 1) + 60), "white")
    d = ImageDraw.Draw(img)
    d.text((40, 40), "Pelican Bay Utilities - Lift Station 7 - material list", fill="black")
    if note:
        d.text((40, 70), note, fill="black")
    cols = [40, 200, 330, 470] if not numbered else [40, 150, 310, 440, 580]
    for i, h in enumerate(header):
        d.text((cols[i], top - 30), h, fill="black")
    d.line([(30, top - 6), (w - 30, top - 6)], fill="black", width=2)
    for r, row in enumerate(rows):
        y = top + r * row_h + 14
        for i, cell in enumerate(row):
            d.text((cols[i], y), str(cell), fill="black")
        d.line([(30, y + row_h - 14), (w - 30, y + row_h - 14)], fill=(180, 180, 180))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def build_extra(inbox: Path) -> None:
    # S011 — a PO that accepts part of our quote BY DESCRIPTION, not by number.
    # Named in DECISIONS.md as an expected failure; never actually run until now.
    (inbox / "S011.eml").write_bytes(
        msg(
            "Janelle Okoye <janelle@brannock.example>",
            "Re: Quote Q-26-04501 - booster skid",
            "Tara,\n\nGo ahead with the flanges and the pipe. Hold the valves and the gaskets "
            "until the engineer signs off. PO to follow.\n\nJanelle\n\n"
            "On Wed, Nov 4, 2026 at 8:15 AM Tara Linscott <tara.linscott@ridgeline-supply.example> wrote:\n"
            "> Hi Janelle,\n"
            "> Here is quote Q-26-04501 for the booster skid.\n"
            ">\n"
            "> 1. 8 EA - 3\" 150# RF weld neck flange, A105, STD bore - $38.60 ea\n"
            "> 2. 120 FT - 3\" SCH 40 A53B ERW pipe, PE - $7.95/ft\n"
            "> 3. 4 EA - 3\" 150# wafer butterfly valve, CF8M, lever - $212.00 ea\n"
            "> 4. 8 EA - 3\" spiral wound gasket, 316/graphite, 150# - $5.10 ea\n"
            ">\n"
            "> Tara Linscott\n"
            "> Inside Sales | Ridgeline Industrial Supply\n",
            date="Wed, 04 Nov 2026 14:00:00 -0600",
            mid="<s011@brannock.example>",
            in_reply_to="<q26-04501@ridgeline-supply.example>",
        ).as_bytes()
    )

    # S012 — a scanned table with NO item-number column.  Strip de-duplication
    # anchors on that index; without one, an overlapping row could double-count.
    sizes = ['1/2"', '3/4"', '1"', '1-1/4"', '1-1/2"', '2"', '2-1/2"', '3"']
    prods = ["90 elbow", "45 elbow", "tee", "coupling", "union", "cap"]
    rows = [[(i * 3) % 40 + 2, "EA", sizes[i % 8], prods[i % 6]] for i in range(24)]
    png = _table_png(
        rows,
        header=["QTY", "UOM", "SIZE", "DESCRIPTION"],
        note="ALL FITTINGS THREADED 3000# A105 UNLESS NOTED",
        numbered=False,
    )
    (inbox / "S012.eml").write_bytes(
        msg(
            "Dale Thibodeaux <dale@pelicanbay.example>",
            "lift station 7 - scanned material list",
            "Scanned the list from the job folder. Please price it all.\n\nDale",
            date="Thu, 05 Nov 2026 11:00:00 -0600",
            mid="<s012@pelicanbay.example>",
            attachments=[("scan_ls7.png", "image/png", png)],
        ).as_bytes()
    )
    return {r[3] + r[2]: r[0] for r in rows}


EXPECT = {
    "S001": "forwarded email: the BOM is attached to the INNER message (3 items)",
    "S002": "two attachments share one filename; both sheets must survive (6 items)",
    "S003": "windows-1252 body with em dash / accents (2 items)",
    "S004": "out-of-office -> auto_reply, no items",
    "S005": "original request (3 items)",
    "S006": "revision 1 -> complete order, line 1 becomes 26 (3 items)",
    "S007": "revision 2 -> builds on revision 1: drop line 3, add a union (3 items)",
    "S008": "same subject as S009 but a DIFFERENT order (1 item)",
    "S009": "must NOT be marked a duplicate of S008 (1 item)",
    "S010": "units GAL/BAG/CS/PR/DZ, 'twelve', '1,200', '2.5'",
    "S011": "PO accepting part of a quote BY DESCRIPTION: flanges + pipe only (2 items, not 4)",
    "S012": "scanned table with NO item-number column, 24 rows (24 items, no repeats)",
}


def main() -> int:
    _load_dotenv()
    tmp = Path(tempfile.mkdtemp(prefix="rfq-shapes-"))
    try:
        inbox = tmp / "emails"
        inbox.mkdir()
        build(inbox)
        build_extra(inbox)

        parsed, cost, results = run(
            inbox, cache_dir=REPO / "cache", api_key=os.getenv("OPENAI_API_KEY"), concurrency=4
        )
        by_id = {r.id: r for r in results}

        for eid in sorted(parsed):
            row, res = parsed[eid], by_id[eid]
            print(f"\n{eid}  — {EXPECT[eid]}")
            docs = ", ".join(f"{d.source}:{d.kind}" for d in res.documents)
            print(f"   class={row['classification']}  review={row['needs_review']}"
                  f"  dup={row['duplicate_of']}  revises={row['revises']}  lines={len(row['line_items'])}")
            print(f"   docs: {docs}")
            if row["needs_review"]:
                print(f"   reason: {(row['review_reason'] or '')[:150]}")
            for l in row["line_items"]:
                print(f"     qty={str(l['qty']):<6} {str(l['uom']):<5} {l['description'][:66]}")

        print(f"\n{len(parsed)} emails, {cost['model_calls']} calls, ${cost['cold_run']['cost_usd']:.4f}")
        return 0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
