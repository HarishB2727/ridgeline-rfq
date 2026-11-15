#!/usr/bin/env python3
"""Score parsed.json against an answer key, in the order the brief says it grades.

    python scripts/score.py parsed.json data/dev/answer_key.json [--detail E005]

Headline numbers first — silent losses, hallucinated lines, lost and phantom
requests — then classification, line recall/precision, and the paired-line
fields including "context carried" (must_mention / must_not_mention).

The equivalence table below mirrors the one the README describes, so a
description that says "Schedule 40" satisfies a key asking for "sch 40".
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ITEM_CLASSES = {"rfq", "rfq_revision", "purchase_order"}

# order matters: longer phrases first
_SYNONYMS = [
    (r"\bthreaded\s*(?:&|and)\s*coupled\b|\bthd\s*(?:&|and)\s*cpld\b|\bt\s*&\s*c\b|\bt\s*/\s*c\b", " tc "),
    (r"\bspiral\s*wound\b|\bspw\b", " spw "),
    (r"\bbutt\s*weld(?:ed|ing)?\b|\bbw\b", " bw "),
    (r"\bsocket\s*weld(?:ed|ing)?\b|\bsw\b", " sw "),
    (r"\bcarbon\s*steel\b", " cs "),
    (r"\bmalleable(?:\s*iron)?\b|\bmall\s*iron\b", " mi "),
    (r"\bfull\s*port\b", " fp "),
    (r"\braised\s*face\b", " rf "),
    (r"\bschedule\b|\bsched\b", " sch "),
    (r"\bseamless\b|\bsmls\b", " smls "),
    (r"\bgalvani[sz]ed\b|\bgalv\b", " galv "),
    (r"\bthreaded\b|\bthrd\b|\bthd\b|\bnpt\b", " npt "),
    (r"\belbow\b|\bell\b", " ell "),
    (r"\bvalve\b|\bvlv\b", " vlv "),
    (r"\bflange\b|\bflg\b", " flg "),
    (r"\bgasket\b|\bgskt\b", " gskt "),
    (r"\breducer\b|\bred\b", " red "),
    (r"\bcoupling\b|\bcplg\b", " cplg "),
    (r"\bclass\s*(\d+)\b", r" \1# "),
    (r"\b(\d+)\s*(?:lbs?|#)\b", r" \1# "),
]


def _fold(text: str) -> str:
    s = " " + (text or "").lower() + " "
    return s.replace("”", '"').replace("“", '"').replace("″", '"').replace("’", "'")


def plain(text: str) -> str:
    """The README's rule on its own: case, spaces and punctuation ignored."""
    return re.sub(r"[^a-z0-9]+", "", _fold(text))


def canon(text: str) -> str:
    """plain(), plus the spelling equivalences the README lists."""
    s = _fold(text)
    # multiword synonyms must survive a hyphen or slash joining their words
    # ("butt-weld" and "butt weld" are the same spec)
    s = re.sub(r"[-_/]+", " ", s)
    for pat, rep in _SYNONYMS:
        s = re.sub(pat, rep, s)
    return re.sub(r"[^a-z0-9]+", "", s)


_TOK = re.compile(r"[a-z0-9]+(?:[./][a-z0-9]+)*")


def toks(text: str) -> set[str]:
    return set(_TOK.findall((text or "").lower()))


def sim(a: str, b: str) -> float:
    ta, tb = toks(a), toks(b)
    if not ta or not tb:
        return 0.0
    inter = len(ta & tb)
    return 2 * inter / (len(ta) + len(tb))


def pair_lines(pred: list[dict], gold: list[dict], floor: float = 0.45):
    """Greedy best-first pairing on raw-token overlap."""
    cands = []
    for i, p in enumerate(pred):
        for j, g in enumerate(gold):
            s = sim(p.get("raw", ""), g.get("raw", ""))
            if s >= floor:
                cands.append((s, i, j))
    cands.sort(reverse=True)
    used_p, used_g, pairs = set(), set(), []
    for s, i, j in cands:
        if i in used_p or j in used_g:
            continue
        used_p.add(i)
        used_g.add(j)
        pairs.append((i, j, s))
    unmatched_p = [i for i in range(len(pred)) if i not in used_p]
    unmatched_g = [j for j in range(len(gold)) if j not in used_g]
    return pairs, unmatched_p, unmatched_g


def mentions(desc: str, phrase: str) -> bool:
    """A phrase counts as mentioned under either normalisation: punctuation-stripped
    (the README's own rule) or synonym-canonical (its list of equal spellings)."""
    dc, dp = canon(desc), plain(desc)
    return any(
        canon(alt) in dc or plain(alt) in dp for alt in phrase.split("|") if alt.strip()
    )


def qty_ok(pred, gold, tol) -> bool:
    if tol is None:
        return True
    if gold is None:
        return pred is None
    if pred is None:
        return False
    if tol == 0:
        return abs(float(pred) - float(gold)) < 1e-9
    return abs(float(pred) - float(gold)) <= abs(float(gold)) * float(tol)


def score(parsed: dict, key: dict, detail: str | None = None) -> dict:
    gold_emails = key["emails"] if "emails" in key else key

    headline = {"silent_losses": [], "hallucinated_lines": [], "lost_requests": [], "phantom_requests": []}
    cls_right = 0
    confusion: dict[str, int] = {}
    tp = fp = fn = 0
    fields = {"qty": [0, 0], "uom": [0, 0], "source": [0, 0], "context": [0, 0]}
    links = {"revises": [0, 0], "duplicate_of": [0, 0]}
    review = {"tp": 0, "fp": 0, "fn": 0}
    per_email = {}

    for eid, g in gold_emails.items():
        p = parsed.get(eid)
        if p is None:
            headline["silent_losses"].append(f"{eid} (missing from output)")
            fn += len(g["line_items"])
            continue

        g_needs = bool(g.get("needs_review"))
        g_lines = g["line_items"]
        p_lines = p.get("line_items") or []
        p_needs = bool(p.get("needs_review"))

        # 1 — silent loss
        if (g_lines or g_needs) and not p_lines and not p_needs:
            headline["silent_losses"].append(eid)

        # 3 — lost / phantom
        if g["classification"] in ITEM_CLASSES and p.get("classification") not in ITEM_CLASSES:
            headline["lost_requests"].append(f"{eid}: {g['classification']} -> {p.get('classification')}")
        if g["classification"] not in ITEM_CLASSES and p.get("classification") in ITEM_CLASSES and p_lines:
            headline["phantom_requests"].append(f"{eid}: {g['classification']} -> {p.get('classification')}")

        if p.get("classification") == g["classification"]:
            cls_right += 1
        else:
            confusion[f"{g['classification']} -> {p.get('classification')}"] = (
                confusion.get(f"{g['classification']} -> {p.get('classification')}", 0) + 1
            )

        pairs, un_p, un_g = pair_lines(p_lines, g_lines)
        tp += len(pairs)
        fp += len(un_p)
        fn += len(un_g)
        for i in un_p:
            headline["hallucinated_lines"].append(f"{eid}: {p_lines[i].get('raw', '')[:70]}")

        for i, j, _s in pairs:
            pl, gl = p_lines[i], g_lines[j]
            fields["qty"][1] += 1
            fields["qty"][0] += qty_ok(pl.get("qty"), gl.get("qty"), gl.get("qty_tol", 0))
            fields["uom"][1] += 1
            fields["uom"][0] += (pl.get("uom") or "EA") == (gl.get("uom") or "EA")
            fields["source"][1] += 1
            fields["source"][0] += (pl.get("source") or "") == (gl.get("source") or "")
            desc_c = pl.get("description", "")
            ok = all(mentions(desc_c, m) for m in gl.get("must_mention") or []) and not any(
                mentions(desc_c, m) for m in gl.get("must_not_mention") or []
            )
            fields["context"][1] += 1
            fields["context"][0] += ok

        for fld in ("revises", "duplicate_of"):
            links[fld][1] += 1
            links[fld][0] += (p.get(fld) or None) == (g.get(fld) or None)

        if g_needs and p_needs:
            review["tp"] += 1
        elif p_needs and not g_needs:
            review["fp"] += 1
        elif g_needs and not p_needs:
            review["fn"] += 1

        per_email[eid] = {
            "class": f"{g['classification']}{'' if p.get('classification') == g['classification'] else ' -> ' + str(p.get('classification'))}",
            "lines": f"{len(p_lines)}/{len(g_lines)}",
            "paired": len(pairs),
            "missed": [g_lines[j]["raw"][:60] for j in un_g],
            "extra": [p_lines[i].get("raw", "")[:60] for i in un_p],
            "needs_review": f"{p_needs}/{g_needs}",
        }

    n = len(gold_emails)
    prec = tp / (tp + fp) if tp + fp else 0.0
    rec = tp / (tp + fn) if tp + fn else 0.0
    return {
        "headline": {
            "silent_losses": len(headline["silent_losses"]),
            "hallucinated_lines": len(headline["hallucinated_lines"]),
            "lost_requests": len(headline["lost_requests"]),
            "phantom_requests": len(headline["phantom_requests"]),
            "_detail": headline,
        },
        "classification": {"accuracy": round(cls_right / n, 4), "right": cls_right, "of": n, "confusion": confusion},
        "lines": {
            "precision": round(prec, 4),
            "recall": round(rec, 4),
            "f1": round(2 * prec * rec / (prec + rec), 4) if prec + rec else 0.0,
            "tp": tp, "fp": fp, "fn": fn,
        },
        "paired_fields": {
            k: {"accuracy": round(v[0] / v[1], 4) if v[1] else None, "of": v[1]} for k, v in fields.items()
        },
        "links": {k: {"accuracy": round(v[0] / v[1], 4) if v[1] else None, "of": v[1]} for k, v in links.items()},
        "needs_review": {
            **review,
            "precision": round(review["tp"] / (review["tp"] + review["fp"]), 4) if review["tp"] + review["fp"] else None,
            "recall": round(review["tp"] / (review["tp"] + review["fn"]), 4) if review["tp"] + review["fn"] else None,
        },
        "per_email": per_email,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("parsed", type=Path)
    ap.add_argument("key", type=Path)
    ap.add_argument("--detail", help="print the per-line breakdown for one email id")
    ap.add_argument("--json", action="store_true", help="dump the full result as JSON")
    a = ap.parse_args()

    parsed = json.loads(a.parsed.read_text(encoding="utf-8"))
    key = json.loads(a.key.read_text(encoding="utf-8"))
    res = score(parsed, key, a.detail)

    if a.json:
        print(json.dumps(res, indent=2, ensure_ascii=False))
        return 0

    h = res["headline"]
    print("HEADLINE (lower is better)")
    print(f"  silent losses       {h['silent_losses']}   {h['_detail']['silent_losses']}")
    print(f"  hallucinated lines  {h['hallucinated_lines']}")
    print(f"  lost requests       {h['lost_requests']}   {h['_detail']['lost_requests']}")
    print(f"  phantom requests    {h['phantom_requests']}   {h['_detail']['phantom_requests']}")
    c = res["classification"]
    print(f"\nclassification  {c['accuracy']:.1%}  ({c['right']}/{c['of']})  {c['confusion'] or ''}")
    l = res["lines"]
    print(f"lines           P {l['precision']:.1%}  R {l['recall']:.1%}  F1 {l['f1']:.1%}   (tp {l['tp']} fp {l['fp']} fn {l['fn']})")
    pf = res["paired_fields"]
    print(
        "paired fields   "
        + "  ".join(f"{k} {v['accuracy']:.1%}" for k, v in pf.items() if v["accuracy"] is not None)
    )
    lk = res["links"]
    print("links           " + "  ".join(f"{k} {v['accuracy']:.1%}" for k, v in lk.items() if v["accuracy"] is not None))
    r = res["needs_review"]
    print(f"needs_review    P {r['precision']}  R {r['recall']}  (tp {r['tp']} fp {r['fp']} fn {r['fn']})")

    print("\nPER EMAIL")
    for eid, row in res["per_email"].items():
        flag = "  " if row["class"].find("->") < 0 and not row["missed"] and not row["extra"] else "! "
        print(f"{flag}{eid}  {row['class']:<34} lines {row['lines']:<7} review {row['needs_review']}")
        for m in row["missed"]:
            print(f"      MISSED {m}")
        for e in row["extra"]:
            print(f"      EXTRA  {e}")

    if a.detail:
        gold = (key.get("emails") or key)[a.detail]
        got = parsed.get(a.detail, {})
        print(f"\n--- {a.detail} ---")
        print(json.dumps(got, indent=2, ensure_ascii=False)[:6000])
        print("--- key ---")
        print(json.dumps(gold, indent=2, ensure_ascii=False)[:6000])
    return 0


if __name__ == "__main__":
    sys.exit(main())
