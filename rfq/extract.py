"""Stage 4 — extraction.  One model call per document (text or picture).

Per-document rather than per-email buys four things: `source` is right by
construction, a 300-page PDF cannot blow up one prompt, documents extract in
parallel, and a document that fails takes only itself down.

Every call also receives the email's subject and body as *ambient context*,
because context and quantity often live apart from the list — a photo of a
nameplate carries the specification while the body carries "quote 3 of these".
What keeps that from leaking into invented lines is not the prompt but the
groundedness check in verify.py.
"""

from __future__ import annotations

from .llm import image_part, text_part
from .prompts import EXTRACT_IMAGE_SYSTEM, EXTRACT_TEXT_SYSTEM, REVISE_SYSTEM, ambient_context
from .schemas import EXTRACT_SCHEMA, REVISE_SCHEMA

MAX_DOC_CHARS = 60_000
CHUNK_CHARS = 18_000


def _chunks(text: str) -> list[str]:
    """Split a long document on blank lines / sheet breaks, never mid-row."""
    if len(text) <= CHUNK_CHARS:
        return [text]
    out, cur = [], []
    size = 0
    for line in text.splitlines(keepends=True):
        if size + len(line) > CHUNK_CHARS and cur:
            out.append("".join(cur))
            cur, size = [], 0
        cur.append(line)
        size += len(line)
    if cur:
        out.append("".join(cur))
    return out


def extract_document(email, doc, documents, runner, *, text_model: str, vision_model: str) -> dict:
    """Return {document_role, lines[], notes, unresolvable_reference} for one document."""
    ctx = ambient_context(email, documents)

    if doc.kind == "image":
        parts = [
            text_part(
                ctx
                + f"\n=== THE PICTURE: {doc.source} ===\n"
                + (f"({len(doc.images)} pages follow)\n" if len(doc.images) > 1 else "")
            )
        ]
        for img in doc.images:
            parts.append(image_part(img, doc.media_type))
        return runner.complete_json(
            email_id=email.id,
            model=vision_model,
            system=EXTRACT_IMAGE_SYSTEM,
            parts=parts,
            schema=EXTRACT_SCHEMA,
            schema_name="document_line_items",
            max_tokens=8000,
        )

    text = doc.text[:MAX_DOC_CHARS]
    pieces = _chunks(text)
    merged = {"document_role": "no_items", "notes": "", "unresolvable_reference": None, "lines": []}
    for i, piece in enumerate(pieces):
        label = doc.source if len(pieces) == 1 else f"{doc.source} (part {i + 1} of {len(pieces)})"
        head = ctx + f"\n=== THE DOCUMENT: {label} ===\n"
        if i and pieces[0]:
            # carry the document's own opening lines into later chunks: that is where
            # the sheet title and the "unless noted" defaults live
            head += "--- opening lines of this document, for context ---\n"
            head += "\n".join(pieces[0].splitlines()[:8]) + "\n--- continues ---\n"
        res = runner.complete_json(
            email_id=email.id,
            model=text_model,
            system=EXTRACT_TEXT_SYSTEM,
            parts=[text_part(head + piece)],
            schema=EXTRACT_SCHEMA,
            schema_name="document_line_items",
            max_tokens=12000,
        )
        merged["lines"].extend(res.get("lines") or [])
        if res.get("document_role") == "item_list":
            merged["document_role"] = "item_list"
        elif res.get("document_role") in ("decorative", "unreadable") and len(pieces) == 1:
            merged["document_role"] = res["document_role"]
        merged["unresolvable_reference"] = merged["unresolvable_reference"] or res.get("unresolvable_reference")
        if res.get("notes"):
            merged["notes"] = (merged["notes"] + " " + res["notes"]).strip()
    return merged


def merge_revision(email, prior_lines: list[dict], runner, *, model: str) -> dict:
    """Apply a reply's changes to the earlier request and return the complete order."""
    import json

    prior = json.dumps(
        [
            {k: ln.get(k) for k in ("raw", "description", "qty", "uom", "source")}
            for ln in prior_lines
        ],
        indent=2,
        ensure_ascii=False,
    )
    body = email.body_current
    quoted = email.body_quoted[:6000]
    parts = [
        text_part(
            "=== THE EARLIER REQUEST, AS RECORDED ===\n"
            f"{prior}\n\n"
            "=== THE CUSTOMER'S REPLY (the current message) ===\n"
            f"Subject: {email.subject}\n{body}\n\n"
            "=== THE EARLIER MESSAGE AS QUOTED IN THE REPLY (context) ===\n"
            f"{quoted or '(none)'}\n"
        )
    ]
    return runner.complete_json(
        email_id=email.id,
        model=model,
        system=REVISE_SYSTEM,
        parts=parts,
        schema=REVISE_SCHEMA,
        schema_name="revised_order",
        max_tokens=12000,
    )
