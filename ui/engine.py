"""Drives the pipeline one email at a time so the viewer can show it happening.

This is not a second implementation. It calls the same `process_email`, the same
`link_inbox`, the same `resolve_revisions` that `rfq parse` calls, in the same
order. What it adds is bookkeeping: which step each email is on, and — through
the ModelRunner's observer — exactly what was sent to the model and what came
back, so the page can put the prompt next to the email next to the result.

Sequential on purpose. `rfq parse` runs six emails at once because it is trying
to finish; this is trying to be watched.
"""

from __future__ import annotations

import re
import sys
import threading
import time
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from rfq.cli import _load_dotenv, default_cache  # noqa: E402
from rfq.ingest import ingest  # noqa: E402
from rfq.link import ITEM_CLASSES, apply_needs_review, link_inbox  # noqa: E402
from rfq.llm import ModelRunner  # noqa: E402
from rfq.pipeline import (  # noqa: E402
    DEFAULT_CLASSIFY_MODEL,
    DEFAULT_EXTRACT_MODEL,
    DEFAULT_VISION_MODEL,
    process_email,
    resolve_revisions,
)

STEPS = [
    ("ingest", "Open the envelope", "split the current message from the thread below it"),
    ("documents", "Read the attachments", "every file becomes text or pictures"),
    ("classify", "What kind of email?", "one of seven — and a gate: four of them stop here"),
    ("extract", "Read each document", "one model call per document"),
    ("verify", "Check every line", "does it really occur in the document it claims?"),
    ("link", "Place it in the inbox", "duplicate? revision? does a human need to look?"),
]

_DOC = re.compile(r"=== THE (DOCUMENT|PICTURE): (.+?) ===")


def _label(rec: dict) -> tuple[str, str]:
    """A human name for a model call, and which document it was about."""
    schema, user = rec.get("schema", ""), "\n".join(rec.get("user") or [])
    if schema.startswith("email_classification"):
        second = "second_look" in schema
        return ("Decide what this email is" + (" (second look)" if second else ""), "")
    if schema == "revised_order":
        return ("Apply the customer's changes to the earlier order", "body")
    m = _DOC.search(user)
    where = m.group(2) if m else "body"
    kind = "picture" if (m and m.group(1) == "PICTURE") else "document"
    return (f"Read the {kind}: {where}", where)


class Job:
    """All the state the page polls for."""

    def __init__(self, job_id: str, inbox: Path, queue: list[dict]) -> None:
        self.id = job_id
        self.inbox = inbox
        self.state = "staged"
        self.started: float | None = None
        self.elapsed = 0.0
        self.error: str | None = None
        self.order = [q["id"] for q in queue]
        self.queue = {q["id"]: q for q in queue}
        self.emails: dict[str, dict] = {
            q["id"]: {
                "id": q["id"],
                "state": "pending",
                "steps": {name: "pending" for name, _, _ in STEPS},
                "calls": [],
                "result": None,
                "summary": None,
                "error": None,
            }
            for q in queue
        }
        self.cost: dict | None = None
        self.results: list = []
        self.lock = threading.Lock()

    def snapshot(self) -> dict:
        with self.lock:
            return {
                "job": self.id,
                "state": self.state,
                "error": self.error,
                "elapsed": round((time.time() - self.started) if self.started else 0.0, 1),
                "order": self.order,
                "queue": [self.queue[i] for i in self.order],
                "emails": {
                    k: {
                        "id": v["id"], "state": v["state"], "steps": v["steps"],
                        "summary": v["summary"], "error": v["error"],
                        "calls": [
                            {"label": c["label"], "model": c["model"], "cached": c["cached"],
                             "usage": c["usage"], "cost_usd": c["cost_usd"]}
                            for c in v["calls"]
                        ],
                    }
                    for k, v in self.emails.items()
                },
                "cost": self.cost,
                "steps": [{"key": k, "title": t, "note": n} for k, t, n in STEPS],
            }


def stage(job_id: str, inbox: Path) -> list[dict]:
    """Read the headers only — no model, no cost — so the queue can be shown first."""
    queue = []
    for path in sorted(inbox.glob("*.eml")):
        row = {"id": path.stem, "file": path.name, "bytes": path.stat().st_size}
        try:
            e = ingest(path)
            row.update(
                sender=f"{e.sender_name} <{e.sender_addr}>".strip(),
                subject=e.subject or "(no subject)",
                date=e.headers.get("date"),
                attachments=[a.filename for a in e.attachments],
                has_thread=bool(e.body_quoted),
                from_us=e.from_us,
                body_chars=len(e.body_current or ""),
                quoted_chars=len(e.body_quoted or ""),
                person=e.sender_name or e.sender_addr.split("@")[0],
                # the organisation, from the domain rather than the signature block:
                # a signature is prose and parsing it guesses, a domain is a fact
                org=(e.sender_domain.split(".")[0] or e.sender_domain).replace("-", " ").title(),
                domain=e.sender_domain,
            )
        except Exception as exc:
            row.update(sender="?", subject=f"(could not be parsed: {exc})", attachments=[])
        queue.append(row)
    return queue


def process(job: Job) -> None:
    threading.Thread(target=_run, args=(job,), daemon=True).start()


def _run(job: Job) -> None:
    job.state = "running"
    job.started = time.time()
    try:
        _load_dotenv()
        import os

        runner = ModelRunner(default_cache(), api_key=os.getenv("OPENAI_API_KEY"))
        models = {
            "classify": DEFAULT_CLASSIFY_MODEL,
            "extract": DEFAULT_EXTRACT_MODEL,
            "vision": DEFAULT_VISION_MODEL,
        }
        current = {"id": None}

        def observe(rec: dict) -> None:
            # The call carries the email it was made for, which is also right during
            # phase B — a revision's merge happens after the per-email loop has ended,
            # so trusting the loop variable loses it.
            eid = rec.get("email_id") or current["id"]
            if eid is None or eid not in job.emails:
                return
            label, where = _label(rec)
            rec = {**rec, "label": label, "about": where}
            with job.lock:
                row = job.emails[eid]
                row["calls"].append(rec)
                # The first model call means steps 1 and 2 are behind us.
                if rec["schema"].startswith("email_classification"):
                    row["steps"]["ingest"] = "done"
                    row["steps"]["documents"] = "done"
                    row["steps"]["classify"] = "done"
                    row["steps"]["extract"] = "running"
                else:
                    row["steps"]["extract"] = "running"

        runner.observer = observe

        results = []
        for eid in job.order:
            path = job.inbox / f"{eid}.eml"
            current["id"] = eid
            with job.lock:
                row = job.emails[eid]
                row["state"] = "running"
                row["steps"]["ingest"] = "running"
                row["steps"]["documents"] = "running"

            r = process_email(path, runner, models)
            results.append(r)

            with job.lock:
                row = job.emails[eid]
                for key in ("ingest", "documents", "classify"):
                    row["steps"][key] = "done"
                row["steps"]["extract"] = "done" if r.classification in ITEM_CLASSES else "skipped"
                row["steps"]["verify"] = "done"
                row["state"] = "linking"
                row["summary"] = {
                    "classification": r.classification,
                    "why": r.classification_why,
                    "n_items": len(r.lines),
                    "n_dropped": len(r.dropped),
                    "documents": [
                        {"source": d.source, "kind": d.kind, "role": r.doc_roles.get(d.source),
                         "error": d.error, "truncated": d.truncated}
                        for d in r.documents
                    ],
                }
        current["id"] = None

        # phase B — needs the whole inbox, so it runs once, in date order
        results.sort(key=lambda r: r.sort_key())
        link_inbox(results)
        resolve_revisions(results, runner, models)
        for r in results:
            apply_needs_review(r)

        with job.lock:
            for r in results:
                row = job.emails.get(r.id)
                if not row:
                    continue
                row["steps"]["link"] = "done"
                row["state"] = "done"
                row["result"] = r.to_json()
                row["trace"] = r.trace()
                row["summary"] = {
                    **(row["summary"] or {}),
                    "classification": r.classification,
                    "n_items": len(r.lines),
                    "needs_review": r.needs_review,
                    "review_reason": r.review_reason,
                    "duplicate_of": r.duplicate_of,
                    "revises": r.revises,
                }
                if r.errors:
                    row["error"] = "; ".join(r.errors)[:400]
            job.results = results
            job.cost = runner.ledger.summary(len(results))
            job.state = "done"
    except Exception as exc:
        job.state = "error"
        job.error = f"{type(exc).__name__}: {exc}"
        job.trace = traceback.format_exc(limit=5)
    finally:
        job.elapsed = round(time.time() - (job.started or time.time()), 1)
