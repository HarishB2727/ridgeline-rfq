"""Stage 6 — threads, duplicates and the review flag.

Runs over the whole inbox in ascending Date order, which is what makes this a
single pass: the spec defines the duplicate as the one with the *later* Date,
and a revision always arrives after the request it revises.

Every rule here is code.  `needs_review` in particular is a set of structural
conditions, not a model opinion — that is what makes "no silent losses" a
property of the system rather than a hope about a prompt.
"""

from __future__ import annotations

import re

from .ingest import normalised_body, normalised_subject
from .verify import tokens

ITEM_CLASSES = {"rfq", "rfq_revision", "purchase_order"}


def _line_signature(lines: list[dict]) -> tuple:
    return tuple(sorted((tuple(tokens(l["raw"])), l.get("qty")) for l in lines))


def _attachment_set(email) -> frozenset[str]:
    return frozenset(a.sha256 for a in email.attachments if a.size > 0)


_SAYS_RESEND = re.compile(
    r"\b(re-?send(ing|t)?|sending (this )?again|sent (this )?again|second request|2nd request|"
    r"disregard if|in case (it|this|the first|you)|did you (get|receive)|duplicate of)\b", re.I)


def _says_resend(result) -> bool:
    """Does the message itself say it is the same request again?

    The classifier is already asked this and answers `looks_like_resend`; the
    regex is the code-side second opinion on the subject and the covering note.
    """
    if (result.cls or {}).get("looks_like_resend"):
        return True
    text = f"{result.email.subject}\n{result.email.body_current}"
    return bool(_SAYS_RESEND.search(text))


def find_duplicate(result, seen: list) -> object | None:
    """The same request arriving twice.

    Requires the same sender and the same subject once Re:/FW:/RESEND: are stripped,
    plus content equality by at least one of three independent routes.  Body text is
    the weakest of the three — a resend usually rewrites the covering note while
    attaching byte-identical files, so the attachment hashes do the real work.
    """
    if result.classification == "rfq_revision":
        return None  # a revision changes the order; it is never a duplicate of it
    subj = normalised_subject(result.email.subject)
    atts = _attachment_set(result.email)
    body = normalised_body(result.email.body_current)
    sig = _line_signature(result.lines)

    resend = _says_resend(result)

    for prior in seen:
        if prior.duplicate_of:
            continue
        if prior.email.sender_addr != result.email.sender_addr:
            continue
        if prior.classification != result.classification:
            continue
        same_subject = normalised_subject(prior.email.subject) == subj
        same_files = bool(atts) and atts == _attachment_set(prior.email)

        # Byte-identical files from the same sender, where the message calls itself
        # a resend, is conclusive on its own.  Requiring the subject to match too
        # loses "PO 7781 - resending" against "PO 7781", which is the commonest
        # way a resend actually arrives.
        if same_files and (same_subject or resend):
            return prior
        if not same_subject:
            continue
        if body and normalised_body(prior.email.body_current) == body:
            return prior
        if sig and _line_signature(prior.lines) == sig:
            return prior
    return None


def find_revised(result, seen: list) -> object | None:
    """The earlier request this reply changes, if it is in this inbox."""
    email = result.email
    refs = f"{email.headers.get('in_reply_to') or ''} {email.headers.get('references') or ''}"
    subj = normalised_subject(email.subject)

    by_header = [p for p in seen if p.email.headers.get("message_id") and p.email.headers["message_id"] in refs]
    if by_header:
        return by_header[-1]

    same_thread = [
        p
        for p in seen
        if p.email.sender_domain == email.sender_domain
        and normalised_subject(p.email.subject) == subj
        and p.classification in ITEM_CLASSES
    ]
    return same_thread[-1] if same_thread else None


def apply_needs_review(result) -> None:
    """Decide whether a human has to look before anyone quotes this.

    Only meaningful on the item-carrying classes; the format does not allow it
    elsewhere.  Each reason below is a thing we could not read or could not resolve,
    never a thing we merely found hard.
    """
    if result.classification not in ITEM_CLASSES or result.duplicate_of:
        result.needs_review = False
        result.review_reason = None
        return

    reasons: list[str] = []

    for doc in result.documents:
        if doc.kind == "unreadable":
            reasons.append(f"{doc.source} could not be read: {doc.error}")
        elif doc.truncated:
            total, read = doc.meta.get("rows_total"), doc.meta.get("rows_read")
            if total and read:
                reasons.append(
                    f"{doc.source} is too long to read in full: {read} of {total} rows were read, "
                    f"the remaining {total - read} were not"
                )
            else:
                pages, got = doc.meta.get("pages"), doc.meta.get("pages_read")
                reasons.append(
                    f"{doc.source} is too long to read in full: {got} of {pages} pages were read"
                    if pages and got
                    else f"{doc.source} was only partly read"
                )

    promised = result.email.signals["mentions_attachment"] or result.email.signals["mentions_picture"]
    item_docs = [d for d in result.documents if d.source != "body" and d.kind != "unreadable"]
    carried_items = [d for d in item_docs if result.doc_roles.get(d.source) == "item_list"]

    # A note reported while reading ONE document ("the items are in the attachment") is
    # scoped to that document.  If another document in the same email did supply the
    # items, the reference resolved — the email as a whole is complete.  Only a reference
    # that nothing in the email could satisfy is a reason to stop a quote.
    #
    # The same applies across emails: a revision that says "change line 1" points at a
    # request this message does not contain, which is an unresolvable reference right up
    # until we find that request in the inbox and merge against it.  Once `revises` is
    # set, it is resolved.
    if result.unresolvable_reference and not carried_items and not result.revises:
        reasons.append(result.unresolvable_reference)

    for err in result.errors:
        reasons.append(err)

    if not result.lines:
        if promised and not carried_items:
            reasons.append(
                "the message refers to an attached list, but no attachment carrying items was found"
            )
        else:
            reasons.append("no line items could be read from this message")
    elif promised and item_docs and not carried_items and not result.lines_from_attachments:
        reasons.append(
            "the message refers to an attached list, but none of the attachments contained one"
        )

    result.needs_review = bool(reasons)
    result.review_reason = "; ".join(dict.fromkeys(reasons))[:500] or None


def link_inbox(results: list) -> None:
    """results must already be sorted by Date ascending."""
    seen: list = []
    for r in results:
        dup = find_duplicate(r, seen)
        if dup is not None:
            r.duplicate_of = dup.id
            r.classification = dup.classification
            r.lines = []
        seen.append(r)
