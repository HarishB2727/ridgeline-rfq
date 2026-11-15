"""Orchestration.

Phase A runs per email and is parallel: ingest, documentize, classify, extract,
verify.  Nothing in it looks at another email.

Phase B runs once over the inbox in Date order and is sequential: duplicates,
revisions (which need the earlier email's lines), and the review flag.

Every email is processed inside an error boundary.  A failure — a corrupt
attachment, a model timeout, a cache miss with no key — becomes a flagged
record and the run carries on.  The output always has one entry per .eml.
"""

from __future__ import annotations

import os
import traceback
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from . import classify as classify_mod
from . import link as link_mod
from .documents import Document, documentize_all
from .extract import extract_document, merge_revision
from .ingest import ParsedEmail, ingest
from .llm import CacheMiss, ModelRunner, ModelUnavailable
from .verify import strip_internal, verify_lines

DEFAULT_CLASSIFY_MODEL = os.getenv("RFQ_MODEL_CLASSIFY", "gpt-4.1-mini")
DEFAULT_EXTRACT_MODEL = os.getenv("RFQ_MODEL_EXTRACT", "gpt-4.1")
DEFAULT_VISION_MODEL = os.getenv("RFQ_MODEL_VISION", "gpt-4.1")


@dataclass
class Result:
    id: str
    email: ParsedEmail | None = None
    documents: list[Document] = field(default_factory=list)
    classification: str = "rfq"
    classification_why: str = ""
    cls: dict = field(default_factory=dict)
    lines: list[dict] = field(default_factory=list)
    doc_roles: dict = field(default_factory=dict)
    haystacks: dict = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    unresolvable_reference: str | None = None
    duplicate_of: str | None = None
    revises: str | None = None
    needs_review: bool = False
    review_reason: str | None = None
    dropped: list[dict] = field(default_factory=list)

    @property
    def lines_from_attachments(self) -> bool:
        return any(l.get("source") != "body" for l in self.lines)

    def sort_key(self):
        if self.email and self.email.date:
            return (self.email.date, self.id)
        return (datetime.min.replace(tzinfo=timezone.utc), self.id)

    def to_json(self) -> dict:
        return {
            "classification": self.classification,
            "revises": self.revises,
            "duplicate_of": self.duplicate_of,
            "needs_review": bool(self.needs_review),
            "review_reason": self.review_reason if self.needs_review else None,
            "line_items": [strip_internal(l) for l in self.lines],
        }

    def trace(self) -> dict:
        return {
            "id": self.id,
            "from": self.email.sender_addr if self.email else None,
            "subject": self.email.subject if self.email else None,
            "date": self.email.headers.get("date") if self.email else None,
            "classification": self.classification,
            "why": self.classification_why,
            "classifier": self.cls,
            "documents": [
                {
                    "source": d.source,
                    "kind": d.kind,
                    "role": self.doc_roles.get(d.source),
                    "error": d.error,
                    "truncated": d.truncated,
                    "meta": d.meta,
                }
                for d in self.documents
            ],
            "duplicate_of": self.duplicate_of,
            "revises": self.revises,
            "needs_review": self.needs_review,
            "review_reason": self.review_reason,
            "errors": self.errors,
            "dropped_lines": self.dropped,
            "line_items": self.lines,
        }


# --------------------------------------------------------------------------- #
# phase A — one email, no knowledge of any other
# --------------------------------------------------------------------------- #

_BODY_HEAD = "=== EMAIL BODY (current message) ==="


def build_body_document(email: ParsedEmail, cls: dict) -> Document:
    """The body as a Document.

    Quoted history is excluded by default.  That single decision is what stops a
    status inquiry with a whole RFQ pasted underneath from becoming eight phantom
    lines — and because the groundedness check only ever sees what we included,
    history cannot sneak back in through the model either.

    It is included in exactly two situations, both of which are about the current
    message: our own rep forwarding a customer (the forwarded text *is* the
    request), and a buyer accepting named items that live only in our quote.
    """
    blocks = [_BODY_HEAD, email.body_current or "(empty)"]

    if email.body_quoted:
        if cls.get("is_rep_forward_of_customer_email"):
            blocks += [
                "",
                "=== MESSAGE FORWARDED BY OUR REP — this is the customer's own request, read it ===",
                email.body_quoted,
            ]
        elif cls.get("selects_items_from_quoted"):
            labels = ", ".join(cls.get("selected_item_labels") or []) or "the ones named above"
            blocks += [
                "",
                f"=== OUR EARLIER QUOTE — the current message commits to items {labels}. "
                "Return ONLY those items, exactly as they are written here. ===",
                email.body_quoted,
            ]
    return Document(source="body", kind="text", text="\n".join(blocks))


def process_email(path: Path, runner: ModelRunner, models: dict) -> Result:
    result = Result(id=path.stem)
    try:
        email = ingest(path)
    except Exception as exc:
        result.errors.append(f"email could not be parsed: {type(exc).__name__}: {exc}")
        result.classification = "rfq"
        result.needs_review = True
        result.review_reason = result.errors[-1]
        return result

    result.email = email
    att_docs = [d for a in email.attachments for d in documentize_all(a)]
    result.documents = att_docs

    # ---- classify ---------------------------------------------------------- #
    try:
        cls = classify_mod.classify(email, att_docs, runner, models["classify"])
        if cls.get("disagreement"):
            cls = classify_mod.second_opinion(email, att_docs, cls, runner, models["extract"])
    except (CacheMiss, ModelUnavailable) as exc:
        result.errors.append(f"could not classify: {exc}")
        result.classification = "rfq"  # degrade towards "a human looks", never towards silence
        result.needs_review = True
        result.review_reason = result.errors[-1]
        return result
    except Exception as exc:
        result.errors.append(f"classifier failed: {type(exc).__name__}: {exc}")
        result.classification = "rfq"
        result.needs_review = True
        result.review_reason = result.errors[-1]
        return result

    result.cls = cls
    result.classification = cls["classification"]
    result.classification_why = cls.get("why", "")
    result.unresolvable_reference = cls.get("unresolvable_reference")

    if result.classification not in link_mod.ITEM_CLASSES:
        return result  # nothing to extract; this is the cost gate

    # ---- extract ----------------------------------------------------------- #
    body_doc = build_body_document(email, cls)
    result.documents = [body_doc] + att_docs
    result.haystacks["body"] = body_doc.text

    to_read = [d for d in result.documents if d.kind != "unreadable"]
    # A revision's body is handled in phase B, against the earlier email's lines.
    if result.classification == "rfq_revision":
        to_read = [d for d in to_read if d.source != "body"]

    for doc in to_read:
        try:
            res = extract_document(
                email, doc, result.documents, runner,
                text_model=models["extract"], vision_model=models["vision"],
            )
        except (CacheMiss, ModelUnavailable) as exc:
            result.errors.append(f"{doc.source} could not be read by the model: {exc}")
            continue
        except Exception as exc:
            result.errors.append(f"{doc.source} failed: {type(exc).__name__}: {exc}")
            continue

        role = res.get("document_role", "no_items")
        result.doc_roles[doc.source] = role
        result.unresolvable_reference = result.unresolvable_reference or res.get("unresolvable_reference")
        if role in ("decorative", "unreadable"):
            # "unreadable" only means something when there were pixels to read.  A text
            # body that is nothing but inline-image placeholders is not a failure — the
            # images themselves are separate documents and get read on their own.
            if role == "unreadable" and doc.kind == "image":
                result.errors.append(f"{doc.source} was unreadable to the model: {res.get('notes', '')[:200]}")
            continue

        lines = res.get("lines") or []
        if doc.kind != "image":
            result.haystacks[doc.source] = doc.text

        if res.get("_mixed"):
            # One document, two halves: the text layer can be checked against itself,
            # the pictures cannot.  Verify each against what it actually came from
            # rather than letting one half borrow the other's evidence.
            from_text = [l for l in lines if l.get("_from") == "text"]
            from_image = [l for l in lines if l.get("_from") != "text"]
            result.lines += verify_lines(
                from_text, source=doc.source, haystack=doc.text, trace=result.dropped
            )
            result.lines += verify_lines(
                from_image,
                source=doc.source,
                haystack=None,
                trace=result.dropped,
                dedupe_by_item_no=bool(res.get("_strip_split")),
            )
        else:
            result.lines += verify_lines(
                lines,
                source=doc.source,
                haystack=doc.text if doc.kind == "text" else None,
                trace=result.dropped,
                dedupe_by_item_no=bool(res.get("_strip_split")),
            )

    return result


# --------------------------------------------------------------------------- #
# phase B — the inbox, in order
# --------------------------------------------------------------------------- #


def resolve_revisions(results: list[Result], runner: ModelRunner, models: dict) -> None:
    seen: list[Result] = []
    for r in results:
        if r.classification == "rfq_revision" and not r.duplicate_of:
            prior = link_mod.find_revised(r, seen)
            r.revises = prior.id if prior else None
            prior_lines = list(prior.lines) if prior else []
            hay = [r.email.body_current, r.email.body_quoted]

            if not prior_lines and r.email.body_quoted:
                # The request being revised is not in this inbox — rebuild it from
                # the history the customer quoted back to us.
                quoted_doc = Document(
                    source="body", kind="text",
                    text="=== THE EARLIER REQUEST, AS QUOTED BY THE CUSTOMER ===\n" + r.email.body_quoted,
                )
                try:
                    res = extract_document(
                        r.email, quoted_doc, r.documents, runner,
                        text_model=models["extract"], vision_model=models["vision"],
                    )
                    prior_lines = verify_lines(
                        res.get("lines") or [], source="body",
                        haystack=quoted_doc.text, trace=r.dropped,
                    )
                except Exception as exc:
                    r.errors.append(f"could not rebuild the earlier request: {type(exc).__name__}: {exc}")

            try:
                merged = merge_revision(r.email, prior_lines, runner, model=models["extract"])
            except (CacheMiss, ModelUnavailable) as exc:
                r.errors.append(f"could not apply the revision: {exc}")
                r.lines = prior_lines + r.lines
                seen.append(r)
                continue
            except Exception as exc:
                r.errors.append(f"revision merge failed: {type(exc).__name__}: {exc}")
                r.lines = prior_lines + r.lines
                seen.append(r)
                continue

            hay.extend(l["raw"] for l in prior_lines)
            hay.extend(l["description"] for l in prior_lines)
            # a merged line keeps the source of the line it came from, so recover it
            # by text rather than by position (verification may drop some)
            src_by_raw = {
                (ln.get("raw") or "").strip(): (ln.get("source") or "body")
                for ln in merged.get("lines") or []
            }
            merged_lines = verify_lines(
                merged.get("lines") or [], source="body",
                haystack="\n".join(h for h in hay if h), trace=r.dropped,
            )
            for ln in merged_lines:
                ln["source"] = src_by_raw.get(ln["raw"], "body")
            # attachment lines extracted in phase A stay as they are
            r.lines = merged_lines + [l for l in r.lines if l.get("source") != "body"]
        seen.append(r)


def run(
    emails_dir: Path,
    *,
    cache_dir: Path,
    api_key: str | None = None,
    models: dict | None = None,
    concurrency: int = 6,
    refresh: bool = False,
    write_cache: bool = True,
) -> tuple[dict, dict, list[Result]]:
    models = models or {
        "classify": DEFAULT_CLASSIFY_MODEL,
        "extract": DEFAULT_EXTRACT_MODEL,
        "vision": DEFAULT_VISION_MODEL,
    }
    runner = ModelRunner(cache_dir, api_key=api_key, refresh=refresh, write_cache=write_cache)

    paths = sorted(Path(emails_dir).glob("*.eml"))
    if not paths:
        raise SystemExit(f"no .eml files found in {emails_dir}")

    results: list[Result] = []

    def one(p: Path) -> Result:
        try:
            return process_email(p, runner, models)
        except Exception as exc:  # the boundary of last resort
            r = Result(id=p.stem)
            r.errors.append(f"unhandled: {type(exc).__name__}: {exc}\n{traceback.format_exc(limit=3)}")
            r.classification = "rfq"
            r.needs_review = True
            r.review_reason = r.errors[-1][:300]
            return r

    if concurrency > 1:
        with ThreadPoolExecutor(max_workers=concurrency) as pool:
            results = list(pool.map(one, paths))
    else:
        results = [one(p) for p in paths]

    results.sort(key=lambda r: r.sort_key())
    link_mod.link_inbox(results)
    resolve_revisions(results, runner, models)
    for r in results:
        link_mod.apply_needs_review(r)

    parsed = {r.id: r.to_json() for r in sorted(results, key=lambda r: r.id)}
    cost = runner.ledger.summary(len(results))
    cost["models"] = models
    cost["offline_replay"] = runner.offline
    return parsed, cost, results
