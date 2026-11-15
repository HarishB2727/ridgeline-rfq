"""Stage 2 — documentize.  Attachments in, uniform Documents out.  Still no model.

Everything an email carries becomes the same thing: a Document with a `source`
(the attachment filename, or "body"), and either text or images.  Because the
Document knows its own name, `source` on every extracted line is correct by
construction rather than by asking a model to remember filenames.

Routing is by magic bytes, never by extension — a held-out inbox will have a
.xls that is really a .csv and a .pdf that is really a photo.
"""

from __future__ import annotations

import csv
import io
import os
import re
import zipfile
from dataclasses import dataclass, field

MAX_IMAGE_SIDE = 2000
MAX_PASSTHROUGH_BYTES = 1_500_000
DEFAULT_PAGE_CAP = 20
SCAN_TEXT_THRESHOLD = 40  # alphanumeric chars per page below which we call it a scan


@dataclass
class Document:
    source: str
    kind: str  # "text" | "image" | "unreadable"
    text: str = ""
    images: list[bytes] = field(default_factory=list, repr=False)
    media_type: str = "image/png"
    error: str | None = None
    truncated: bool = False
    meta: dict = field(default_factory=dict)

    @property
    def readable(self) -> bool:
        return self.kind != "unreadable"


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #


def sniff(data: bytes, mime: str, filename: str) -> str:
    """Return a routing key from content first, declared type second, name last."""
    head = data[:8]
    if head.startswith(b"%PDF"):
        return "pdf"
    if head.startswith(b"\xff\xd8\xff"):
        return "image"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image"
    if head[:6] in (b"GIF87a", b"GIF89a") or head.startswith(b"BM"):
        return "image"
    if head.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "image"
    if head.startswith(b"PK\x03\x04"):
        try:
            names = set(zipfile.ZipFile(io.BytesIO(data)).namelist())
        except Exception:
            return "unknown"
        if any(n.startswith("xl/") for n in names):
            return "xlsx"
        if any(n.startswith("word/") for n in names):
            return "docx"
        return "unknown"
    if head.startswith(b"\xd0\xcf\x11\xe0"):
        return "legacy_office"
    if mime == "message/rfc822" or filename.lower().endswith(".eml"):
        return "eml"
    if mime == "text/csv" or filename.lower().endswith((".csv", ".tsv")):
        return "csv"
    if mime.startswith("text/"):
        return "text"
    # last resort: does it decode as text?
    try:
        data[:4000].decode("utf-8")
        return "text"
    except UnicodeDecodeError:
        return "unknown"


def _cell(value) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip()


def _row(cells: list[str]) -> str:
    while cells and not cells[-1]:
        cells.pop()
    return " | ".join(cells)


# --------------------------------------------------------------------------- #
# per-format readers
# --------------------------------------------------------------------------- #


def read_xlsx(data: bytes, source: str) -> Document:
    from openpyxl import load_workbook

    wb = load_workbook(io.BytesIO(data), data_only=True)
    out: list[str] = []
    skipped: list[str] = []
    for ws in wb.worksheets:
        # A hidden sheet is the author telling you it is not the live version
        # (E005 hides "Rev A (old)" behind the Rev B everyone is meant to read).
        if ws.sheet_state != "visible":
            skipped.append(ws.title)
            continue
        out.append(f"=== SHEET: {ws.title} ===")
        for row in ws.iter_rows(values_only=True):
            line = _row([_cell(c) for c in row])
            if line.strip():
                out.append(line)
        out.append("")
    doc = Document(source=source, kind="text", text="\n".join(out).strip())
    doc.meta["sheets"] = [ws.title for ws in wb.worksheets if ws.sheet_state == "visible"]
    doc.meta["hidden_sheets_skipped"] = skipped
    return doc


def read_docx(data: bytes, source: str) -> Document:
    import docx
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    d = docx.Document(io.BytesIO(data))
    out: list[str] = []
    for child in d.element.body.iterchildren():
        tag = child.tag.rsplit("}", 1)[-1]
        if tag == "p":
            txt = Paragraph(child, d).text.strip()
            if txt:
                out.append(txt)
        elif tag == "tbl":
            for r in Table(child, d).rows:
                line = _row([c.text.strip() for c in r.cells])
                if line.strip():
                    out.append(line)
            out.append("")
    return Document(source=source, kind="text", text="\n".join(out).strip())


def read_csv(data: bytes, source: str) -> Document:
    text = data.decode("utf-8-sig", "replace")
    sample = text[:4000]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel
    rows = list(csv.reader(io.StringIO(text), dialect))
    lines = [_row([c.strip() for c in r]) for r in rows]
    return Document(source=source, kind="text", text="\n".join(l for l in lines if l.strip()))


def _page_lines(page) -> str:
    """Rebuild a page's table structure from word boxes.

    A text PDF's raw get_text() flattens columns into runs of spaces; grouping
    words by baseline and splitting on horizontal gaps restores 'cell | cell',
    which is both what the answer key's `raw` looks like and what a model reads
    a BOM from most reliably.
    """
    words = page.get_text("words")  # (x0, y0, x1, y1, word, block, line, word_no)
    if not words:
        return ""
    rows: dict[int, list[tuple]] = {}
    for w in words:
        rows.setdefault(round(w[1] / 3.0), []).append(w)
    out = []
    for key in sorted(rows):
        ws = sorted(rows[key], key=lambda w: w[0])
        cells, cur, prev_end = [], [ws[0][4]], ws[0][2]
        for w in ws[1:]:
            if w[0] - prev_end > 8.0:
                cells.append(" ".join(cur))
                cur = [w[4]]
            else:
                cur.append(w[4])
            prev_end = w[2]
        cells.append(" ".join(cur))
        out.append(" | ".join(c.strip() for c in cells if c.strip()))
    return "\n".join(l for l in out if l.strip())


MIN_EMBEDDED_PX = 200
MIN_EMBEDDED_AREA_FRAC = 0.08


def _substantial_images(doc, page) -> list[bytes]:
    """Embedded rasters big enough to be a document rather than a logo or a rule.

    Returned as their own images, not as a render of the whole page: the picture
    occupies a region the text layer does not describe, so reading both cannot
    double-count the same row.
    """
    from PIL import Image

    out: list[bytes] = []
    page_area = abs(page.rect.width * page.rect.height) or 1.0
    for info in page.get_images(full=True):
        xref = info[0]
        try:
            rects = page.get_image_rects(xref)
        except Exception:
            rects = []
        shown = sum(abs(r.width * r.height) for r in rects)
        if shown / page_area < MIN_EMBEDDED_AREA_FRAC:
            continue
        try:
            ex = doc.extract_image(xref)
        except Exception:
            continue
        if ex.get("width", 0) < MIN_EMBEDDED_PX or ex.get("height", 0) < MIN_EMBEDDED_PX:
            continue
        raw = ex.get("image") or b""
        if not raw:
            continue
        try:  # normalise to PNG so one Document has one media type
            buf = io.BytesIO()
            Image.open(io.BytesIO(raw)).convert("RGB").save(buf, format="PNG")
            out.append(buf.getvalue())
        except Exception:
            continue
    return out


def read_pdf(data: bytes, source: str, page_cap: int = DEFAULT_PAGE_CAP) -> Document:
    import pymupdf

    try:
        doc = pymupdf.open(stream=data, filetype="pdf")
    except Exception as exc:
        return Document(source=source, kind="unreadable", error=f"PDF would not open: {exc}")

    if doc.needs_pass:
        return Document(
            source=source,
            kind="unreadable",
            error="PDF is password protected and cannot be opened",
        )

    n_pages = doc.page_count
    pages = min(n_pages, page_cap)
    truncated = n_pages > pages

    # Decide PER PAGE, not per file.  A PDF is routinely a mixture: a typed covering
    # page in front of photographed ones, or a typed note with a screenshot of the BOM
    # pasted into it.  Judging the whole file on its average text density reads the
    # covering note and throws the actual order away.
    texts: list[str] = []
    images: list[bytes] = []
    text_pages, scanned_pages, embedded = 0, 0, 0

    for i in range(pages):
        page = doc[i]
        t = _page_lines(page)
        if sum(ch.isalnum() for ch in t) >= SCAN_TEXT_THRESHOLD:
            texts.append(f"--- page {i + 1} ---\n{t}")
            text_pages += 1
            # a page can carry text AND a picture of a table; read the picture too
            for img in _substantial_images(doc, page):
                images.append(img)
                embedded += 1
        else:
            zoom = min(2.5, MAX_IMAGE_SIDE / max(page.rect.width, page.rect.height))
            images.append(page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom)).tobytes("png"))
            scanned_pages += 1

    body = "\n".join(texts)
    kind = "text" if not images else ("image" if not body.strip() else "mixed")
    d = Document(
        source=source,
        kind=kind,
        text=body,
        images=images,
        media_type="image/png",
        truncated=truncated,
    )
    d.meta.update(
        pages=n_pages,
        pages_read=pages,
        text_pages=text_pages,
        scanned_pages=scanned_pages,
        embedded_images=embedded,
    )
    return d


def read_image(data: bytes, source: str, mime: str) -> Document:
    from PIL import Image

    try:
        im = Image.open(io.BytesIO(data))
        im.load()
    except Exception as exc:
        return Document(source=source, kind="unreadable", error=f"image would not decode: {exc}")

    w, h = im.size
    if max(w, h) <= MAX_IMAGE_SIDE and len(data) <= MAX_PASSTHROUGH_BYTES:
        media = mime if mime.startswith("image/") else "image/png"
        d = Document(source=source, kind="image", images=[data], media_type=media)
        d.meta.update(width=w, height=h, resized=False)
        return d

    scale = MAX_IMAGE_SIDE / max(w, h)
    im = im.convert("RGB").resize((max(1, int(w * scale)), max(1, int(h * scale))), Image.LANCZOS)
    buf = io.BytesIO()
    im.save(buf, format="JPEG", quality=85)
    d = Document(source=source, kind="image", images=[buf.getvalue()], media_type="image/jpeg")
    d.meta.update(width=w, height=h, resized=True)
    return d


def read_eml(data: bytes, source: str) -> Document:
    """An attached message — a bounce's original, or a forwarded .eml.

    Rendered with its own envelope so the classifier can see *whose* message it is.
    A bounce carries our own outbound RFQ; those are not customer requests.
    """
    import base64

    from .ingest import ingest_bytes

    def _parse(raw: bytes):
        try:
            return ingest_bytes(raw, f"{source}:inner")
        except Exception:
            return None

    inner = _parse(data)
    # RFC 2046 says message/rfc822 must be 7bit, so the stdlib will not record a
    # base64 Content-Transfer-Encoding for one — but plenty of gateways base64 it
    # regardless, and then the payload decodes to base64 text instead of a message.
    # A message with neither From nor Subject is the tell.
    if inner is None or not (inner.headers.get("from") or inner.subject):
        try:
            decoded = base64.b64decode(data, validate=False)
        except Exception:
            decoded = b""
        if decoded:
            again = _parse(decoded)
            if again is not None and (again.headers.get("from") or again.subject):
                inner = again

    if inner is None:
        return Document(source=source, kind="unreadable", error="attached message could not be parsed")

    head = [
        f"From: {inner.headers.get('from')}",
        f"To: {inner.headers.get('to')}",
        f"Date: {inner.headers.get('date')}",
        f"Subject: {inner.headers.get('subject')}",
    ]
    text = "=== ATTACHED MESSAGE ===\n" + "\n".join(head) + "\n\n" + inner.body_current
    d = Document(source=source, kind="text", text=text.strip())
    d.meta.update(
        attached_message=True,
        inner_from=inner.sender_addr,
        inner_from_us=inner.from_us,
        inner_subject=inner.subject,
        inner_attachment_names=[a.filename for a in inner.attachments],
    )
    # The forwarded message carries its own attachments, and that is usually where
    # the order is: a rep forwards "please price the attached" and the attachment
    # hangs off the INNER message, not the one that reached us.  Reading only the
    # inner body finds a covering note and no items.
    d.meta["_inner_attachments"] = inner.attachments
    return d


# --------------------------------------------------------------------------- #
# the router
# --------------------------------------------------------------------------- #


MAX_DOC_ROWS = int(os.getenv("RFQ_MAX_DOC_ROWS", "800"))


def cap_rows(doc: Document, limit: int = MAX_DOC_ROWS) -> Document:
    """Refuse to read an unbounded document — but say so.

    A 3,000-line BOM has to stop somewhere: reading it whole costs several dollars
    for one email and overruns the model's output budget. The cap is not the
    interesting part; the flag is. Truncating in silence returns a partial order
    that looks exactly like a complete one, which is a silent loss wearing the
    clothes of a successful parse.
    """
    if doc.kind not in ("text", "mixed") or not doc.text:
        return doc
    lines = doc.text.splitlines()
    if len(lines) <= limit:
        return doc
    doc.text = "\n".join(lines[:limit])
    doc.truncated = True
    doc.meta.update(rows_total=len(lines), rows_read=limit)
    return doc


def documentize(attachment, page_cap: int = DEFAULT_PAGE_CAP) -> Document:
    """One attachment -> one Document.  Never raises: a failure is a flagged Document."""
    source, data, mime = attachment.filename, attachment.data, attachment.mime
    if not data:
        return Document(source=source, kind="unreadable", error="attachment is empty (0 bytes)")

    kind = sniff(data, mime, source)
    try:
        if kind == "xlsx":
            return cap_rows(read_xlsx(data, source))
        if kind == "docx":
            return cap_rows(read_docx(data, source))
        if kind == "csv":
            return cap_rows(read_csv(data, source))
        if kind == "pdf":
            return cap_rows(read_pdf(data, source, page_cap))
        if kind == "image":
            return read_image(data, source, mime)
        if kind == "eml":
            return read_eml(data, source)
        if kind == "text":
            return cap_rows(Document(source=source, kind="text", text=data.decode("utf-8", "replace").strip()))
        if kind == "legacy_office":
            return Document(
                source=source,
                kind="unreadable",
                error="legacy Office binary format (.xls/.doc) — no reader available",
            )
        return Document(
            source=source, kind="unreadable", error=f"unsupported attachment type ({mime or 'unknown'})"
        )
    except Exception as exc:  # a corrupt file is one flagged line, not a dead run
        return Document(
            source=source, kind="unreadable", error=f"{type(exc).__name__} while reading: {exc}"
        )


STRIP_TARGET_PX = 700
STRIP_OVERLAP = 0.12
MAX_STRIPS = 5


def split_tall(png: bytes, target: int = STRIP_TARGET_PX) -> list[bytes]:
    """Cut a tall page image into overlapping horizontal strips.

    A model reading a full page of a dense table drifts between rows: it pairs a
    quantity with the line above or below it, and on a skewed phone scan it does so
    often.  Measured on the dev inbox's two-page scanned RFQ, reading the page whole
    got 13 of 28 quantities right; the same model on three overlapping strips got
    28 of 28.  A larger model did not fix it — fewer rows per look did.

    The strips overlap so that no row falls in a crack; duplicates that come back
    from the overlap are removed by item number in verify.
    """
    from PIL import Image

    im = Image.open(io.BytesIO(png))
    w, h = im.size
    n = min(MAX_STRIPS, max(1, round(h / target)))
    if n < 2:
        return [png]

    out, step = [], h / n
    for i in range(n):
        top = max(0, int(i * step - STRIP_OVERLAP * step))
        bottom = min(h, int((i + 1) * step + STRIP_OVERLAP * step))
        buf = io.BytesIO()
        im.crop((0, top, w, bottom)).save(buf, format="PNG")
        out.append(buf.getvalue())
    return out


MAX_NESTING = 3


def documentize_all(attachment, page_cap: int = DEFAULT_PAGE_CAP, _depth: int = 0) -> list[Document]:
    """One attachment -> one or more Documents, following forwarded messages inward.

    An attached email is a container, not a leaf.  Its own attachments become
    documents in their own right, under their own filenames, so a line extracted
    from a forwarded spreadsheet reports the spreadsheet as its source rather than
    the envelope it travelled in.  Depth-limited: a chain of forwards is ordinary,
    a thousand nested messages is a denial-of-service.
    """
    doc = documentize(attachment, page_cap)
    inner = doc.meta.pop("_inner_attachments", None)
    out = [doc]
    if inner and _depth < MAX_NESTING:
        for att in inner:
            out.extend(documentize_all(att, page_cap, _depth + 1))
    return out


_LOGO_HINT = re.compile(r"(logo|signature|sig|banner|footer|header|icon|spacer)", re.I)


def looks_decorative(doc: Document, attachment) -> bool:
    """A weak prior only — the vision model gets the final say.

    Deliberately NOT a size rule: in the dev inbox the Delta Mechanical logo is
    3,307 bytes and the spreadsheet screenshot holding six real line items is
    4,295; Cobalt Ridge's logo is 5,288, larger than both.  Any byte threshold
    silently deletes a customer's order.
    """
    return bool(attachment.inline and _LOGO_HINT.search(attachment.filename or ""))
