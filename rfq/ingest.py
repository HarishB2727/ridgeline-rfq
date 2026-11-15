"""Stage 1 — ingest.  RFC-822 bytes in, a normalised ParsedEmail out.  No model, ever.

Two jobs matter here and both are pure code:

  * splitting the CURRENT message from the QUOTED history.  Everything downstream
    trusts that split; it is the single biggest guard against extracting lines out
    of a thread's history (E012 quotes a whole RFQ under a status question).
  * fingerprinting.  Every byte that reaches a model gets hashed so the answer can
    be cached and replayed without a key.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email import policy
from email.parser import BytesParser
from email.utils import parseaddr, parsedate_to_datetime
from html.parser import HTMLParser
from pathlib import Path

OUR_DOMAIN = "ridgeline-supply.example"

HEADER_NAMES = (
    "from", "to", "cc", "date", "subject", "message-id", "in-reply-to", "references",
)


# --------------------------------------------------------------------------- #
# HTML -> text, table aware
# --------------------------------------------------------------------------- #

_BLOCK = {"p", "div", "br", "tr", "li", "h1", "h2", "h3", "h4", "h5", "h6", "table"}
_CELL = {"td", "th"}
_DROP = {"script", "style", "head"}


class _HtmlText(HTMLParser):
    """Renders HTML as text.  Table rows become 'cell | cell | cell' — the shape the
    answer key uses for a pasted table, and the shape a model reads a BOM from."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self._skip = 0
        self._cells: list[str] = []
        self._in_cell = False

    def handle_starttag(self, tag, attrs):
        if tag in _DROP:
            self._skip += 1
            return
        if tag == "img":
            a = dict(attrs)
            src = (a.get("src") or "").removeprefix("cid:")
            self.out.append(f"\n[inline image: {src or a.get('alt') or 'image'}]\n")
            return
        if tag in _CELL:
            self._in_cell = True
            self._cells.append("")
            return
        if tag in _BLOCK:
            self._flush_row()
            self.out.append("\n")

    def handle_endtag(self, tag):
        if tag in _DROP:
            self._skip = max(0, self._skip - 1)
            return
        if tag in _CELL:
            self._in_cell = False
            return
        if tag == "tr":
            self._flush_row()
        elif tag in _BLOCK:
            self.out.append("\n")

    def handle_data(self, data):
        if self._skip:
            return
        if self._in_cell:
            self._cells[-1] += data
        else:
            self.out.append(data)

    def _flush_row(self) -> None:
        if self._cells:
            cells = [re.sub(r"\s+", " ", c).strip() for c in self._cells]
            self._cells = []
            if any(cells):
                self.out.append("\n" + " | ".join(cells) + "\n")

    def text(self) -> str:
        self._flush_row()
        raw = "".join(self.out)
        raw = re.sub(r"[ \t]+", " ", raw)
        raw = re.sub(r"\n{3,}", "\n\n", raw)
        return "\n".join(ln.rstrip() for ln in raw.splitlines()).strip()


def html_to_text(html: str) -> str:
    p = _HtmlText()
    try:
        p.feed(html)
        p.close()
    except Exception:  # malformed HTML is still worth whatever parsed
        pass
    return p.text()


# --------------------------------------------------------------------------- #
# current message vs quoted history
# --------------------------------------------------------------------------- #

_SEP_PATTERNS = (
    re.compile(r"^\s*-{2,}\s*(original|forwarded)\s+message\s*-{2,}", re.I),
    re.compile(r"^\s*-{3,}\s*forwarded by\b", re.I),
    re.compile(r"^\s*_{5,}\s*$"),
    re.compile(r"^\s*>"),
    re.compile(r"^\s*on\s.{0,250}\bwrote:\s*$", re.I),
    re.compile(r"^\s*\*?\s*(begin forwarded message)\s*:?\s*$", re.I),
)
# An Outlook-style header block introducing the quoted message.  Localised because
# a block we do not recognise does not fail loudly — it silently hands last month's
# order to the extractor as if it were today's request.  Safe to widen: a match
# needs the From-like line AND two of the follow-up fields within four lines.
_FROM_WORDS = r"from|von|de|da|van|fra|från|fra|od"
_FOLLOWUP_WORDS = (
    r"sent|date|to|cc|bcc|subject"           # English
    r"|gesendet|an|betreff|datum"            # German
    r"|envoy[ée]|objet|[àa]"                 # French
    r"|enviado|enviada|para|asunto|fecha"    # Spanish / Portuguese
    r"|assunto|inviato|oggetto"              # Portuguese / Italian
    r"|verzonden|aan|onderwerp"              # Dutch
)
_FROM_LINE = re.compile(rf"^\s*\*?\s*({_FROM_WORDS})\s*:\s*\S", re.I)
_FOLLOWUP = re.compile(rf"^\s*\*?\s*({_FOLLOWUP_WORDS})\s*:", re.I)


def split_quoted(text: str) -> tuple[str, str]:
    """Return (current, quoted).  The first line that begins the thread's history wins."""
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if any(p.search(line) for p in _SEP_PATTERNS):
            return "\n".join(lines[:i]).strip(), "\n".join(lines[i:]).strip()
        # "On <date> <person>\nwrote:" wraps across two lines in some clients
        if re.match(r"^\s*on\s", line, re.I) and i + 1 < len(lines):
            joined = f"{line} {lines[i + 1]}".strip()
            if re.search(r"\bwrote:\s*$", joined, re.I) and len(joined) < 300:
                return "\n".join(lines[:i]).strip(), "\n".join(lines[i:]).strip()
        # Outlook header block: From: ... followed by Sent:/To:/Subject:
        if _FROM_LINE.match(line):
            lookahead = lines[i + 1 : i + 5]
            if sum(bool(_FOLLOWUP.match(x)) for x in lookahead) >= 2:
                return "\n".join(lines[:i]).strip(), "\n".join(lines[i:]).strip()
    return text.strip(), ""


# --------------------------------------------------------------------------- #
# model
# --------------------------------------------------------------------------- #


@dataclass
class Attachment:
    filename: str
    mime: str
    size: int
    inline: bool
    content_id: str | None
    data: bytes = field(repr=False, default=b"")

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.data).hexdigest()


@dataclass
class ParsedEmail:
    id: str
    path: Path
    headers: dict[str, str | None]
    date: datetime | None
    sender_name: str
    sender_addr: str
    subject: str
    body_current: str
    body_quoted: str
    attachments: list[Attachment]
    signals: dict
    fingerprint: str

    @property
    def sender_domain(self) -> str:
        return self.sender_addr.rsplit("@", 1)[-1].lower() if "@" in self.sender_addr else ""

    @property
    def from_us(self) -> bool:
        return self.sender_domain.endswith(OUR_DOMAIN)

    def sort_key(self) -> tuple:
        return (self.date or datetime.min.replace(tzinfo=timezone.utc), self.id)


_MENTIONS_ATTACHMENT = re.compile(
    r"\b(attach\w*|enclos\w*|see\s+below\s+file|in\s+the\s+(pdf|file|sheet|spreadsheet)"
    r"|the\s+(pdf|bom|list|form|file|spreadsheet|drawing)\s+(is\s+)?(attached|below))\b",
    re.I,
)
_MENTIONS_PICTURE = re.compile(r"\b(pic|picture|photo|image|screenshot|snapshot|tag|nameplate)\b", re.I)


def _best_text(msg) -> tuple[str | None, str | None]:
    """Walk the MIME tree once; return (text/plain, text/html) of the message body."""
    text = html = None
    stack = [msg]
    while stack:
        part = stack.pop(0)
        if part.get_content_type() == "message/rfc822":
            continue  # an attached message is an attachment, not this message's body
        if part.is_multipart():
            stack[0:0] = list(part.iter_parts())
            continue
        disp = part.get_content_disposition()
        ctype = part.get_content_type()
        if disp in ("attachment", "inline") and (part.get_filename() or not ctype.startswith("text/")):
            continue
        try:
            payload = part.get_content()
        except Exception:
            payload = (part.get_payload(decode=True) or b"").decode("utf-8", "replace")
        if ctype == "text/plain" and text is None:
            text = payload
        elif ctype == "text/html" and html is None:
            html = payload
    return text, html


def _collect_attachments(msg) -> list[Attachment]:
    out: list[Attachment] = []
    stack = [msg]
    while stack:
        part = stack.pop(0)
        ctype = part.get_content_type()
        if ctype == "message/rfc822":
            try:
                data = (
                    part.get_payload(0).as_bytes()
                    if part.is_multipart()
                    else (part.get_payload(decode=True) or b"")
                )
            except Exception:
                data = b""
            out.append(
                Attachment(
                    filename=part.get_filename() or f"attached-{len(out) + 1}.eml",
                    mime=ctype,
                    size=len(data),
                    inline=False,
                    content_id=(part.get("Content-ID") or "").strip("<>") or None,
                    data=data,
                )
            )
            continue
        if part.is_multipart():
            stack[0:0] = list(part.iter_parts())
            continue
        disp = part.get_content_disposition()
        if disp in ("attachment", "inline") and (
            part.get_filename() or not ctype.startswith("text/")
        ):
            data = part.get_payload(decode=True) or b""
            out.append(
                Attachment(
                    filename=part.get_filename() or f"unnamed-{len(out) + 1}",
                    mime=ctype,
                    size=len(data),
                    inline=disp == "inline",
                    content_id=(part.get("Content-ID") or "").strip("<>") or None,
                    data=data,
                )
            )
    return out


def _signals(msg, email_id: str, current: str, atts: list[Attachment], sender: str) -> dict:
    header_blob = "\n".join(f"{k}: {v}" for k, v in msg.items())
    bounce = bool(
        msg.get("X-Failed-Recipients")
        or re.search(r"\b(mailer-daemon|postmaster)\b", sender, re.I)
        or msg.get_content_type() == "multipart/report"
        or re.search(r"report-type=delivery-status", header_blob, re.I)
    )
    return {
        "from_us": sender.rsplit("@", 1)[-1].lower().endswith(OUR_DOMAIN) if "@" in sender else False,
        "bounce_envelope": bounce,
        "auto_submitted": bool(msg.get("Auto-Submitted") and msg.get("Auto-Submitted") != "no"),
        "list_unsubscribe": bool(msg.get("List-Unsubscribe") or re.search(r"^\s*unsubscribe\b", current, re.I | re.M)),
        "precedence_bulk": (msg.get("Precedence") or "").lower() in ("bulk", "list", "junk"),
        "mentions_attachment": bool(_MENTIONS_ATTACHMENT.search(current)),
        "mentions_picture": bool(_MENTIONS_PICTURE.search(current)),
        "attachment_names": [a.filename for a in atts],
        "has_rfc822_attachment": any(a.mime == "message/rfc822" for a in atts),
    }


def ingest(path: Path) -> ParsedEmail:
    return ingest_bytes(path.read_bytes(), path.stem, path)


def ingest_bytes(raw: bytes, email_id: str, path: Path | None = None) -> ParsedEmail:
    msg = BytesParser(policy=policy.default).parsebytes(raw)

    text, html = _best_text(msg)
    # An HTML part with a real table carries structure the flattened text/plain lost
    # (E024's spec lives in a merged header cell).  Otherwise text/plain is the truth.
    if html and re.search(r"<table", html, re.I):
        body = html_to_text(html)
    elif text:
        body = text
    elif html:
        body = html_to_text(html)
    else:
        body = ""

    current, quoted = split_quoted(body)
    atts = _collect_attachments(msg)
    name, addr = parseaddr(msg.get("From") or "")

    try:
        date = parsedate_to_datetime(msg.get("Date")) if msg.get("Date") else None
        if date and date.tzinfo is None:
            date = date.replace(tzinfo=timezone.utc)
    except Exception:
        date = None

    return ParsedEmail(
        id=email_id,
        path=path or Path(email_id),
        headers={h.replace("-", "_"): msg.get(h) for h in HEADER_NAMES},
        date=date,
        sender_name=name,
        sender_addr=addr.lower(),
        subject=(msg.get("Subject") or "").strip(),
        body_current=current,
        body_quoted=quoted,
        attachments=atts,
        signals=_signals(msg, email_id, current, atts, addr.lower()),
        fingerprint=hashlib.sha256(raw).hexdigest(),
    )


def normalised_body(text: str) -> str:
    """For duplicate detection: collapse whitespace and case."""
    return re.sub(r"\s+", " ", text or "").strip().lower()


def normalised_subject(subject: str) -> str:
    s = subject or ""
    while True:
        new = re.sub(r"^\s*(re|fw|fwd|resend|resending|reminder|2nd request)\s*:\s*", "", s, flags=re.I)
        if new == s:
            break
        s = new
    return re.sub(r"\s+", " ", s).strip().lower()
