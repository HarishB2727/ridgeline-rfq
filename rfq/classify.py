"""Stage 3 — classification, and the cost gate.

Four of the seven classes carry no line items by definition, so this call also
decides whether the expensive extraction stage runs at all.  That makes a wrong
answer here a *silent loss*, the most expensive failure there is — hence the
code-computed signals handed to the model, the deliberate asymmetry in the
prompt, and the disagreement check in `reconcile`.
"""

from __future__ import annotations

from .llm import text_part
from .prompts import CLASSIFY_SYSTEM
from .schemas import CLASSIFY_SCHEMA

ITEM_CLASSES = {"rfq", "rfq_revision", "purchase_order"}
MAX_QUOTED_CHARS = 4000


def build_parts(email, documents) -> list[dict]:
    manifest = []
    for d in documents:
        if d.source == "body":
            continue
        if d.kind == "unreadable":
            manifest.append(f"  - {d.source}: COULD NOT BE READ ({d.error})")
        elif d.kind == "image":
            manifest.append(f"  - {d.source}: picture ({len(d.images)} page/image)")
        else:
            head = "\n".join(d.text.splitlines()[:12])
            manifest.append(f"  - {d.source}: document, first lines:\n      " + head.replace("\n", "\n      "))

    sig = email.signals
    priors = []
    if sig["from_us"]:
        priors.append("sender is one of OUR OWN addresses (@ridgeline-supply.example)")
    if sig["bounce_envelope"]:
        priors.append("headers are a delivery-status/bounce envelope")
    if sig["auto_submitted"]:
        priors.append("Auto-Submitted header is set")
    if sig["list_unsubscribe"] or sig["precedence_bulk"]:
        priors.append("bulk-mail headers / an unsubscribe link are present")
    if sig["has_rfc822_attachment"]:
        priors.append("an entire email is attached to this one")
    if email.headers.get("in_reply_to"):
        priors.append("this is a reply (In-Reply-To is set)")

    quoted = email.body_quoted[:MAX_QUOTED_CHARS]
    if len(email.body_quoted) > MAX_QUOTED_CHARS:
        quoted += "\n[...quoted history truncated...]"

    blocks = [
        "=== HEADERS ===",
        f"From: {email.sender_name} <{email.sender_addr}>",
        f"To: {email.headers.get('to')}",
        f"Date: {email.headers.get('date')}",
        f"Subject: {email.subject}",
        "",
        "=== SIGNALS DETECTED BY CODE (facts, not conclusions) ===",
        ("\n".join(f"  - {p}" for p in priors) or "  - none"),
        "",
        "=== CURRENT MESSAGE ===",
        email.body_current or "(empty)",
        "",
        "=== QUOTED EARLIER MESSAGES (history — not the current message) ===",
        quoted or "(none)",
        "",
        "=== ATTACHMENTS ===",
        ("\n".join(manifest) or "  (none)"),
    ]
    return [text_part("\n".join(blocks))]


def classify(email, documents, runner, model: str) -> dict:
    result = runner.complete_json(
        email_id=email.id,
        model=model,
        system=CLASSIFY_SYSTEM,
        parts=build_parts(email, documents),
        schema=CLASSIFY_SCHEMA,
        schema_name="email_classification",
        max_tokens=800,
    )
    return reconcile(email, documents, result)


def _looks_like_an_item_table(documents) -> int:
    """Count rows that look like 'a quantity and a thing' across readable documents.

    Deliberately crude.  It exists only to notice when code and the model disagree
    about whether this email carries anything to price.
    """
    import re

    row = re.compile(r"(^|\|)\s*\(?\d{1,5}\)?\s*(ea|each|jts?|joints?|ft|feet|pcs?|pieces|sets?|rolls?|bx|lb)?\b", re.I)
    n = 0
    for d in documents:
        if d.kind != "text" or not d.text:
            continue
        for line in d.text.splitlines():
            if len(line.strip()) > 8 and row.search(line) and re.search(r"[a-z]{3,}", line, re.I):
                n += 1
    return n


def second_opinion(email, documents, first: dict, runner, model: str) -> dict:
    """Code and the model disagree about whether this email carries items.

    Rather than guess, ask once more with the conflict stated, using the stronger
    model.  `needs_review` is not available here — the format only allows it on
    item-carrying classes — so an explicit re-read is the only honest escalation.
    """
    parts = build_parts(email, documents)
    parts.append(
        text_part(
            "\n=== SECOND LOOK ===\n"
            f"A first pass classified this as {first['classification']!r} because: {first['why']}\n"
            f"But a structural check disagrees: {first['disagreement']}.\n"
            "Decide again. If the attachment is a customer's list of things to price, this is an "
            "rfq (or purchase_order). If it is a supplier's quote to us, a price list, a bounce's "
            "attached copy of our own outbound request, or history already handled, keep the "
            "non-request class and say why."
        )
    )
    again = runner.complete_json(
        email_id=email.id,
        model=model,
        system=CLASSIFY_SYSTEM,
        parts=parts,
        schema=CLASSIFY_SCHEMA,
        schema_name="email_classification_second_look",
        max_tokens=800,
    )
    out = reconcile(email, documents, again)
    out["escalated"] = {"first": first["classification"], "reason": first["disagreement"]}
    return out


def reconcile(email, documents, result: dict) -> dict:
    """Let code veto the model where the headers are unambiguous, and record
    disagreement where they are not."""
    out = dict(result)
    out["code_override"] = None
    out["disagreement"] = None

    # A delivery-status envelope is machine-generated by definition.  This is the
    # one place code outranks the model: a bounce carrying our own outbound RFQ is
    # the classic way to invent a whole phantom order.
    if email.signals["bounce_envelope"] and out["classification"] != "auto_reply":
        out["code_override"] = f"bounce envelope headers overrode model answer {out['classification']!r}"
        out["classification"] = "auto_reply"
        out["carries_items"] = False

    # You cannot revise a thread that is not there.  "Also please add ..." alongside a
    # fresh attachment reads like a change but is just the rest of a first request, and
    # mislabelling it costs the duplicate link too (a revision is never a duplicate).
    if out["classification"] == "rfq_revision":
        has_thread = bool(
            email.headers.get("in_reply_to") or email.headers.get("references") or email.body_quoted
        )
        if not has_thread:
            out["code_override"] = "no reply headers and no quoted history: cannot revise an earlier message"
            out["classification"] = "rfq"
            out["revises_earlier_request"] = False

    if out["classification"] not in ITEM_CLASSES:
        rows = _looks_like_an_item_table(documents)
        if rows >= 3 and not email.signals["bounce_envelope"]:
            out["disagreement"] = (
                f"classified {out['classification']!r} but {rows} rows in the attachments look like "
                "quantity + item"
            )
    out["carries_items"] = out["classification"] in ITEM_CLASSES
    return out
