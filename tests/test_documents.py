#!/usr/bin/env python3
"""The deterministic layer: how bytes become documents.  No model, no key, free.

Everything here was a defect at some point, so everything here is a regression
test. The two at the top were found by building an input shape the dev inbox does
not contain, verified by hand, and then had no test guarding them — which is a fix
waiting to come undone.

The dev-routing block at the end exists for a different reason: it pins the exact
text the committed cache was recorded against. Change how a PDF or a sheet is
rendered and every recorded answer silently stops matching, the keyless replay
breaks, and the run looks fine while calling the model for all 24 emails.

    python tests/test_documents.py
"""

from __future__ import annotations

import io
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from rfq.documents import (  # noqa: E402
    MAX_DOC_ROWS,
    cap_rows,
    documentize,
    documentize_all,
    read_pdf,
    read_xlsx,
    sniff,
)
from rfq.extract import CHUNK_ROWS, _chunks  # noqa: E402
from rfq.ingest import ingest  # noqa: E402

DEV = REPO / "data" / "dev" / "emails"
checks: list[tuple[bool, str]] = []


def check(ok: bool, label: str, detail: str = "") -> None:
    checks.append((bool(ok), f"{label}{('  — ' + detail) if detail else ''}"))


def _sheet(n_rows: int) -> bytes:
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.title = "FITTINGS - SW 3000#"
    ws.append(["Unit 9 - full BOM"])
    ws.append(["ALL FITTINGS SW 3000# A105 UNLESS NOTED"])
    ws.append(["ITEM", "QTY", "UOM", "SIZE", "DESCRIPTION"])
    for i in range(1, n_rows + 1):
        ws.append([i, (i % 40) + 1, "EA", '1-1/2"', "90 Elbow"])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _hybrid_pdf() -> bytes:
    """A typed covering note with a screenshot of a BOM pasted into it."""
    import pymupdf

    shot = ingest(DEV / "E010.eml").attachments[0].data  # a real spreadsheet screenshot
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((60, 70), "Ridgeline RFQ - Job 4412 shutdown materials", fontsize=13)
    page.insert_text((60, 95), "Please quote the items on the sheet below.", fontsize=10)
    page.insert_image(pymupdf.Rect(60, 120, 520, 460), stream=shot)
    page.insert_text((60, 500), "Delivery to Plant 2 dock. Terms net 30.", fontsize=10)
    return doc.tobytes()


class _Att:
    def __init__(self, filename, mime, data):
        self.filename, self.mime, self.data = filename, mime, data
        self.inline, self.content_id, self.size = False, None, len(data)


def main() -> int:
    # ---- a PDF carrying BOTH text and pictures -------------------------------- #
    # Was: any text layer meant "text document" and the pixels were never read, so a
    # screenshot of the order was invisible.  Silent, because the covering note parses.
    d = read_pdf(_hybrid_pdf(), "hybrid.pdf")
    check(d.kind == "mixed", "hybrid PDF is routed as mixed", f"got {d.kind!r}")
    check(len(d.images) >= 1, "its embedded picture is extracted", f"{len(d.images)} image(s)")
    check("Job 4412" in d.text, "its typed text is still read")

    # a logo-sized raster must NOT drag a page into mixed
    import pymupdf

    small = pymupdf.open()
    p = small.new_page()
    p.insert_text((60, 70), "Please quote the list below: 10 ea 2in gate valve, 800#", fontsize=11)
    p.insert_image(pymupdf.Rect(40, 700, 110, 740), stream=ingest(DEV / "E017.eml").attachments[0].data)
    d2 = read_pdf(small.tobytes(), "letterhead.pdf")
    check(d2.kind == "text", "a letterhead logo does not make a page mixed", f"got {d2.kind!r}")

    # ---- a spreadsheet past the row cap --------------------------------------- #
    # Was: truncated at 60k characters inside the extractor with truncated=False,
    # so a third of the order vanished and the email looked like a clean parse.
    big = documentize(_Att("Unit9.xlsx", "application/vnd.ms-excel", _sheet(3000)))
    check(big.truncated, "an oversized sheet is marked truncated")
    check(
        big.meta.get("rows_total", 0) > big.meta.get("rows_read", 0) == MAX_DOC_ROWS,
        "it reports how much it did not read",
        f"{big.meta.get('rows_read')} of {big.meta.get('rows_total')}",
    )
    check(len(big.text.splitlines()) == MAX_DOC_ROWS, "and is actually cut to the cap")

    small_sheet = documentize(_Att("small.xlsx", "application/vnd.ms-excel", _sheet(50)))
    check(not small_sheet.truncated, "an ordinary sheet is not truncated")

    # chunking must be bounded by ROWS: the constraint is the model's output length
    chunks = _chunks(big.text)
    check(
        all(len(c.splitlines()) <= CHUNK_ROWS for c in chunks),
        "no chunk exceeds the row budget",
        f"{len(chunks)} chunks, max {max(len(c.splitlines()) for c in chunks)} rows",
    )

    # ---- a forwarded message is a container, not a leaf ----------------------- #
    bounce = ingest(DEV / "E013.eml")
    docs = documentize_all(bounce.attachments[0])
    check(len(docs) >= 1 and docs[0].meta.get("attached_message"), "an attached .eml is parsed")
    check(
        "_inner_attachments" not in docs[0].meta,
        "its inner-attachment handle is consumed, not leaked into traces",
    )

    # ---- routing by content, never by filename -------------------------------- #
    check(sniff(b"%PDF-1.4\n...", "application/octet-stream", "x.txt") == "pdf", "magic bytes beat the mime type")
    check(sniff(b"Qty,UOM\n1,EA\n", "application/vnd.ms-excel", "list.xlsx") == "csv", "a CSV named .xlsx is a CSV")
    check(sniff(b"\xd0\xcf\x11\xe0" + b"\0" * 20, "application/vnd.ms-excel", "a.xls") == "legacy_office", "legacy .xls is recognised")

    # ---- hidden sheets ---------------------------------------------------------#
    e005 = ingest(DEV / "E005.eml")
    x = read_xlsx(e005.attachments[0].data, "Tank14.xlsx")
    check(x.meta["hidden_sheets_skipped"] == ["Rev A (old)"], "a hidden sheet is skipped")
    check("REV A" not in x.text.upper(), "and none of its rows reach the model")

    # ---- the groundedness check ----------------------------------------------- #
    # The dev inbox no longer provokes this: the prompt fix that stopped the model
    # re-emitting the body's lines while reading an attachment means all 24 traces
    # have an empty dropped_lines.  A guard with no live example is a guard nobody
    # is watching, so it is exercised here instead.
    from rfq.verify import grounded, is_not_a_product, tokens, vague_quantity, verify_lines

    pdf_text = "ITEM | QTY | UOM | SIZE | DESCRIPTION\n1 | 7 | EA | 1/2\" | WELD NECK FLANGE, STD BORE"
    hay = set(tokens(pdf_text))
    check(grounded('1 | 7 | EA | 1/2" | WELD NECK FLANGE, STD BORE', hay)[0], "a real row is grounded")
    check(
        not grounded('2 sets of 4" 150# spiral wound gaskets, 304 SS/graphite', hay)[0],
        "a line from the email body is NOT grounded in the attachment",
        "the exact E006 case, which is how it was found",
    )
    check(not grounded("8 ea - 6\" 300# blind flange, A105", hay)[0], "an invented line is not grounded")

    trace: list = []
    kept = verify_lines(
        [
            {"raw": '1 | 7 | EA | 1/2" | WELD NECK FLANGE, STD BORE', "description": "a", "qty": 7, "uom": "EA"},
            {"raw": '2 sets of 4" spiral wound gaskets', "description": "b", "qty": 2, "uom": "SET"},
            {"raw": "TOTAL | 35", "description": "c", "qty": 35, "uom": "EA"},
        ],
        source="RFQ.pdf", haystack=pdf_text, trace=trace,
    )
    check(len(kept) == 1 and len(trace) == 2, "verify keeps the row and drops the stray and the total",
          f"kept {len(kept)}, dropped {len(trace)}")
    check(is_not_a_product("ITEM | QTY | UOM | SIZE | DESCRIPTION"), "a header row is not a product")
    check(is_not_a_product("Subtotal | 6,030.10"), "a totals row is not a product")
    check(not is_not_a_product('1 | 7 | EA | 1/2" | WELD NECK FLANGE'), "a real row is a product")
    check(vague_quantity("a couple caps"), "'a couple' states no number")
    check(not vague_quantity("twelve lengths of 1in pipe"), "a number spelled out is a number")
    check(not vague_quantity("3 gal thread sealant"), "a digit always wins")

    # ---- the cache contract ----------------------------------------------------#
    # These exact shapes are what the committed answers were recorded against.
    expected = {
        "E005": ("text", 17, 1),   # +1: the tab-name line read_xlsx now emits
        "E006": ("text", 57, 1),
        "E009": ("image", 0, 2),
        "E015": ("text", 19, 1),
        "E016": ("text", 7, 1),
        "E019": ("text", 17, 1),
        "E021": ("text", 57, 1),   # +2: two visible tabs, one such line each
        "E022": ("unreadable", 0, 0),
    }
    for eid, (kind, rows, n) in expected.items():
        e = ingest(DEV / f"{eid}.eml")
        doc = documentize(e.attachments[0])
        got_rows = len(doc.text.splitlines()) if doc.text else 0
        got_n = len(doc.images) if doc.kind == "image" else (1 if doc.kind == "text" else 0)
        check(
            doc.kind == kind and got_rows == rows and got_n == n,
            f"{eid} routes exactly as the cache was recorded",
            f"{doc.kind}/{got_rows} rows/{got_n}",
        )

    width = max(len(m) for _, m in checks) + 2
    for ok, msg in checks:
        print(f"  {'ok  ' if ok else 'FAIL'}  {msg:<{width}}")
    failed = [m for ok, m in checks if not ok]
    print(f"\n{len(checks) - len(failed)}/{len(checks)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
