"""parsed.json -> parties -> requests -> emails.

A derived view, built on top of the graded output and never writing to it.

The reason it exists: `parsed.json` is keyed one row per .eml because that is the
contract, but a rep does not work in emails. E003 and E004 are not two things to
quote; they are one request whose order changed. E006 and E008 are one request
sent twice. The thread links the pipeline already produces — `revises`,
`duplicate_of` — are exactly what collapses twenty-four rows into twenty-two
requests, and the current order is whichever email in the thread spoke last.

No model calls. Re-reads the .eml headers only, which is deterministic and fast.
"""

from __future__ import annotations

import re
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from rfq.ingest import ingest, normalised_subject  # noqa: E402
from rfq.verify import tokens  # noqa: E402

ITEM_CLASSES = {"rfq", "rfq_revision", "purchase_order"}
NO_ACTION = {"auto_reply", "other"}

# A customer's own reference: PF-26-3091, SC-4418, TPS-88213, 26-0412, HM-7716.
# Ours look like Q-26-xxxxx and are deliberately excluded — that is our number,
# not theirs, and showing it as the customer's reference would mislead a rep.
_REF = re.compile(r"\b(?!Q-)([A-Z]{2,5}-\d{1,3}-\d{3,5}|[A-Z]{2,5}-\d{3,6}|\d{2}-\d{4})\b")
_OURS = re.compile(r"\bQ-\d{2}-\d{3,6}\b", re.I)


def _ref(subject: str, body: str) -> str | None:
    for text in (subject or "", (body or "")[:400]):
        m = _REF.search(text)
        if m:
            return m.group(1)
    return None


def _our_quote(subject: str, body: str) -> str | None:
    m = _OURS.search(f"{subject} {body[:600]}")
    return m.group(0).upper() if m else None


def _customer_domain(email) -> str:
    """Whose request is this?

    Mail from one of our own addresses is a rep forwarding a customer in, so the
    party is the person in the forwarded headers, not the colleague who sent it.
    Filing E016 under ridgeline-supply would put our own staff in the customer list.
    """
    if not email.from_us:
        return email.sender_domain
    m = re.search(r"from:.*?@([\w.-]+)", email.body_quoted or "", re.I)
    if m:
        return m.group(1).lower().rstrip(">.,;")
    return email.sender_domain


def _pair(a: list[dict], b: list[dict], floor: float = 0.45):
    """Greedy best-first pairing on raw tokens — used only to describe a change."""
    cands = []
    for i, x in enumerate(a):
        tx = set(tokens(x.get("raw", "")))
        for j, y in enumerate(b):
            ty = set(tokens(y.get("raw", "")))
            if not tx or not ty:
                continue
            s = 2 * len(tx & ty) / (len(tx) + len(ty))
            if s >= floor:
                cands.append((s, i, j))
    cands.sort(reverse=True)
    used_a, used_b, pairs = set(), set(), []
    for s, i, j in cands:
        if i in used_a or j in used_b:
            continue
        used_a.add(i)
        used_b.add(j)
        pairs.append((i, j))
    return pairs, [i for i in range(len(a)) if i not in used_a], [j for j in range(len(b)) if j not in used_b]


def _changes(before: list[dict], after: list[dict]) -> list[dict]:
    """What a revision did, in a rep's words."""
    pairs, gone, added = _pair(before, after)
    out = []
    for i, j in pairs:
        if before[i].get("qty") != after[j].get("qty"):
            out.append({
                "kind": "quantity",
                "text": f"{after[j]['description'][:60]}: {before[i].get('qty')} → {after[j].get('qty')}",
            })
    for i in gone:
        out.append({"kind": "removed", "text": before[i]["description"][:70]})
    for j in added:
        out.append({"kind": "added", "text": f"{after[j].get('qty')} × {after[j]['description'][:60]}"})
    return out


def build(parsed: dict, emails_dir: Path) -> dict:
    mails: dict[str, dict] = {}
    for path in sorted(Path(emails_dir).glob("*.eml")):
        eid = path.stem
        row = parsed.get(eid)
        if row is None:
            continue
        try:
            e = ingest(path)
        except Exception:
            continue
        mails[eid] = {
            "id": eid,
            "domain": _customer_domain(e),
            "contact": e.sender_name or e.sender_addr,
            "address": e.sender_addr,
            "subject": e.subject,
            "date": e.date.isoformat() if e.date else None,
            "sort": e.date.timestamp() if e.date else 0.0,
            "classification": row["classification"],
            "line_items": row["line_items"],
            "n_items": len(row["line_items"]),
            "revises": row.get("revises"),
            "duplicate_of": row.get("duplicate_of"),
            "needs_review": bool(row.get("needs_review")),
            "review_reason": row.get("review_reason"),
            "ref": _ref(e.subject, e.body_current),
            "our_quote": _our_quote(e.subject, f"{e.body_current} {e.body_quoted}"),
            "attachments": [a.filename for a in e.attachments],
            "forwarded_by": e.sender_addr if e.from_us else None,
        }

    # ---- group into threads -------------------------------------------------- #
    def root(eid: str, seen: set | None = None) -> str:
        seen = seen or set()
        m = mails.get(eid)
        if not m or eid in seen:
            return eid
        seen.add(eid)
        for link in (m["revises"], m["duplicate_of"]):
            if link and link in mails:
                return root(link, seen)
        return eid

    threads: dict[tuple, list[str]] = defaultdict(list)
    for eid, m in mails.items():
        r = root(eid)
        # Only item-carrying mail threads. A bounce and a newsletter are not a
        # customer's request and must not sit in the queue looking like one.
        if mails[r]["classification"] in NO_ACTION or mails[r]["classification"] == "vendor_quote":
            key = (mails[eid]["domain"], eid)
        else:
            key = (mails[r]["domain"], normalised_subject(mails[r]["subject"]))
        threads[key].append(eid)

    # ---- one record per request ---------------------------------------------- #
    requests = []
    for (domain, _), ids in threads.items():
        ids.sort(key=lambda i: mails[i]["sort"])
        first, last = mails[ids[0]], mails[ids[-1]]
        classes = {mails[i]["classification"] for i in ids}

        live = [i for i in ids if not mails[i]["duplicate_of"] and mails[i]["n_items"]]
        current_id = live[-1] if live else ids[-1]

        if classes & NO_ACTION and not (classes & ITEM_CLASSES):
            status = "no action"
        elif "purchase_order" in classes:
            status = "ordered"
        elif "vendor_quote" in classes:
            status = "supplier quote"
        elif "rfq_revision" in classes:
            status = "revised"
        elif "status_inquiry" in classes:
            status = "chased"
        else:
            status = "new"

        changes = []
        for i in ids:
            prior = mails[i]["revises"]
            if prior and prior in mails:
                changes = _changes(mails[prior]["line_items"], mails[i]["line_items"])

        requests.append({
            "key": f"{domain}|{normalised_subject(first['subject'])}"[:120],
            "domain": domain,
            "subject": first["subject"],
            "ref": next((mails[i]["ref"] for i in ids if mails[i]["ref"]), None),
            "our_quote": next((mails[i]["our_quote"] for i in ids if mails[i]["our_quote"]), None),
            "status": status,
            "needs_review": any(mails[i]["needs_review"] for i in ids),
            "review_reasons": [mails[i]["review_reason"] for i in ids if mails[i]["review_reason"]],
            "opened": first["date"],
            "last_activity": last["date"],
            "sort": last["sort"],
            "current_from": current_id,
            "line_items": mails[current_id]["line_items"],
            "n_items": mails[current_id]["n_items"],
            "changes": changes,
            "timeline": [
                {k: mails[i][k] for k in
                 ("id", "date", "classification", "n_items", "revises", "duplicate_of",
                  "needs_review", "review_reason", "subject", "contact", "attachments")}
                for i in ids
            ],
        })

    # ---- group requests by party --------------------------------------------- #
    parties: dict[str, dict] = {}
    for r in requests:
        p = parties.setdefault(r["domain"], {"domain": r["domain"], "requests": [], "contacts": []})
        p["requests"].append(r)
    for r in requests:
        for t in r["timeline"]:
            c = t["contact"]
            if c and c not in parties[r["domain"]]["contacts"]:
                parties[r["domain"]]["contacts"].append(c)

    for p in parties.values():
        statuses = {r["status"] for r in p["requests"]}
        # A party's type comes from what they send us, not from their address.
        if statuses == {"supplier quote"}:
            p["type"] = "supplier"
        elif statuses <= {"no action"}:
            p["type"] = "none"
        else:
            p["type"] = "customer"
        p["open_requests"] = sum(1 for r in p["requests"] if r["status"] not in ("no action", "supplier quote"))
        p["needs_review"] = any(r["needs_review"] for r in p["requests"])
        p["requests"].sort(key=lambda r: -r["sort"])

    order = {"customer": 0, "supplier": 1, "none": 2}
    party_list = sorted(parties.values(), key=lambda p: (order[p["type"]], p["domain"]))

    return {
        "parties": party_list,
        "totals": {
            "emails": len(mails),
            "requests": len(requests),
            "customers": sum(1 for p in party_list if p["type"] == "customer"),
            "suppliers": sum(1 for p in party_list if p["type"] == "supplier"),
            "no_action": sum(1 for p in party_list if p["type"] == "none"),
            "needs_review": sum(1 for r in requests if r["needs_review"]),
            "line_items": sum(r["n_items"] for r in requests),
        },
    }


if __name__ == "__main__":
    import argparse
    import json

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("parsed", type=Path)
    ap.add_argument("--emails", type=Path, required=True)
    ap.add_argument("--out", type=Path)
    a = ap.parse_args()

    data = build(json.loads(a.parsed.read_text(encoding="utf-8")), a.emails)
    if a.out:
        a.out.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    t = data["totals"]
    print(f"{t['emails']} emails → {t['requests']} requests · "
          f"{t['customers']} customers, {t['suppliers']} suppliers, {t['no_action']} no-action · "
          f"{t['needs_review']} need review")
    for p in data["parties"]:
        mark = {"customer": " ", "supplier": "S", "none": "-"}[p["type"]]
        print(f"\n{mark} {p['domain']}")
        for r in p["requests"]:
            flag = " ⚠" if r["needs_review"] else ""
            ref = f" [{r['ref']}]" if r["ref"] else ""
            print(f"    {r['status']:<14}{ref} {r['subject'][:44]:<44} {r['n_items']:>3} items"
                  f" (from {r['current_from']}){flag}")
            for e in r["timeline"]:
                tag = (f"dup of {e['duplicate_of']}" if e["duplicate_of"]
                       else f"revises {e['revises']}" if e["revises"] else "")
                print(f"       {e['id']}  {e['classification']:<15}{e['n_items']:>3}  {tag}")
            for c in r["changes"]:
                print(f"         ~ {c['kind']}: {c['text']}")
