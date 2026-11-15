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

CHUNK_CHARS = 18_000
CHUNK_ROWS = 120


def _chunks(text: str) -> list[str]:
    """Split a long document, never mid-row.

    Bounded by ROWS as well as characters, because the binding constraint is the
    model's *output*: every row comes back as a raw, a description and its context,
    roughly 70 tokens. Six hundred rows in one call is a cheap-looking prompt and a
    40,000-token answer that gets cut off at the budget — and a truncated answer
    loses the whole document, not the tail of it.
    """
    lines = text.splitlines(keepends=True)
    if len(lines) <= CHUNK_ROWS and len(text) <= CHUNK_CHARS:
        return [text]
    out, cur, size = [], [], 0
    for line in lines:
        if cur and (len(cur) >= CHUNK_ROWS or size + len(line) > CHUNK_CHARS):
            out.append("".join(cur))
            cur, size = [], 0
        cur.append(line)
        size += len(line)
    if cur:
        out.append("".join(cur))
    return out


def _extract_images(email, doc, ctx, runner, vision_model: str) -> dict:
    """One call per page; a tall page is shown as overlapping strips.

    Both choices exist for the same reason: the fewer rows a model looks at in one
    go, the less it drifts across them.  Keeping pages in separate calls also bounds
    the output length, so a twenty-page scan cannot truncate a single response.
    """
    from .documents import split_tall

    merged = {"document_role": "no_items", "notes": "", "unresolvable_reference": None, "lines": []}
    roles = []

    for page_no, page in enumerate(doc.images, start=1):
        strips = split_tall(page)
        where = doc.source if len(doc.images) == 1 else f"{doc.source}, page {page_no} of {len(doc.images)}"
        note = f"\n=== THE PICTURE: {where} ===\n"
        if len(strips) > 1:
            note += (
                f"This one page is supplied as {len(strips)} overlapping horizontal strips, top to "
                "bottom. They are parts of a single page, not separate documents, and the strips "
                "overlap — so a row near a strip's edge may appear twice. Return every item exactly "
                "once, and skip a row that is cut off at an edge if you cannot read it completely: "
                "it is readable in full in the neighbouring strip.\n"
            )
        if merged["lines"]:
            # A table continued onto a new page keeps the section that was open when
            # the page broke — and that heading is back on the previous page, which
            # this call cannot see.  Hand over the tail of what has been read already.
            tail = merged["lines"][-4:]
            note += (
                "\nRows already read from earlier pages of this same document, oldest first:\n"
                + "\n".join(f"  {l.get('raw', '')}   ->   {l.get('description', '')}" for l in tail)
                + "\nIf this page continues that table, it continues its section too: carry the "
                "same heading and defaults onto these rows unless this page states otherwise, "
                "and do not repeat rows already listed above.\n"
            )

        parts = [text_part(ctx + note)]
        for s in strips:
            parts.append(image_part(s, "image/png" if len(strips) > 1 else doc.media_type))

        res = runner.complete_json(
            email_id=email.id,
            model=vision_model,
            system=EXTRACT_IMAGE_SYSTEM,
            parts=parts,
            schema=EXTRACT_SCHEMA,
            schema_name="document_line_items",
            max_tokens=8000,
        )
        merged["lines"].extend(res.get("lines") or [])
        roles.append(res.get("document_role", "no_items"))
        merged["unresolvable_reference"] = merged["unresolvable_reference"] or res.get("unresolvable_reference")
        if res.get("notes"):
            merged["notes"] = (merged["notes"] + " " + res["notes"]).strip()

    if "item_list" in roles:
        merged["document_role"] = "item_list"
    elif roles and all(r == "decorative" for r in roles):
        merged["document_role"] = "decorative"
    elif roles and all(r == "unreadable" for r in roles):
        merged["document_role"] = "unreadable"
    merged["_strip_split"] = True
    return merged


def extract_document(email, doc, documents, runner, *, text_model: str, vision_model: str) -> dict:
    """Return {document_role, lines[], notes, unresolvable_reference} for one document."""
    ctx = ambient_context(email, documents)

    if doc.kind == "image":
        return _extract_images(email, doc, ctx, runner, vision_model)

    if doc.kind == "mixed":
        # Both halves, merged.  They cover disjoint parts of the file — the text layer
        # and the pictures it does not describe — so neither can repeat the other.
        from_text = _extract_text(email, doc, ctx, runner, text_model)
        from_images = _extract_images(email, doc, ctx, runner, vision_model)
        for ln in from_text.get("lines") or []:
            ln["_from"] = "text"
        for ln in from_images.get("lines") or []:
            ln["_from"] = "image"
        roles = {from_text.get("document_role"), from_images.get("document_role")}
        return {
            "document_role": "item_list" if "item_list" in roles else "no_items",
            "notes": " ".join(x for x in (from_text.get("notes"), from_images.get("notes")) if x),
            "unresolvable_reference": from_text.get("unresolvable_reference")
            or from_images.get("unresolvable_reference"),
            "lines": (from_text.get("lines") or []) + (from_images.get("lines") or []),
            "_strip_split": from_images.get("_strip_split", False),
            "_mixed": True,
        }

    return _extract_text(email, doc, ctx, runner, text_model)


def _extract_text(email, doc, ctx, runner, text_model: str) -> dict:
    text = doc.text  # already row-capped and flagged in documentize()
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
