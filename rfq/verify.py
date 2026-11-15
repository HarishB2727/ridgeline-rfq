"""Stage 5 — verification.  Model output is evidence, not truth.

The model's job was to read.  This file decides what ships.  Three checks matter:

  * groundedness — a line whose words are not in the document it claims to come
    from is dropped.  This is what structurally prevents hallucinated lines,
    rather than hoping a prompt was persuasive enough.
  * vocabulary — uom is forced into the fourteen allowed values or null.
  * non-products — a row whose every word is a heading, total or price label is
    not a thing to quote.
"""

from __future__ import annotations

import re

from .schemas import UOMS

_TOKEN = re.compile(r"[a-z0-9]+(?:[./][a-z0-9]+)*")

UOM_ALIASES = {
    "ea": "EA", "each": "EA", "pc": "EA", "pcs": "EA", "piece": "EA", "pieces": "EA",
    "jt": "EA", "jts": "EA", "joint": "EA", "joints": "EA", "length": "EA", "lengths": "EA",
    "ft": "FT", "feet": "FT", "foot": "FT", "lf": "FT",
    "m": "M", "meter": "M", "metre": "M", "meters": "M",
    "in": "IN", "inch": "IN", "inches": "IN",
    "bx": "BX", "box": "BX", "boxes": "BX",
    "pk": "PK", "pack": "PK", "packs": "PK", "package": "PK",
    "pr": "PR", "pair": "PR", "pairs": "PR",
    "dz": "DZ", "doz": "DZ", "dozen": "DZ",
    "rl": "RL", "roll": "RL", "rolls": "RL",
    "lb": "LB", "lbs": "LB", "pound": "LB", "pounds": "LB",
    "set": "SET", "sets": "SET",
    "gal": "GAL", "gallon": "GAL", "gallons": "GAL",
    "bag": "BAG", "bags": "BAG",
    "cs": "CS", "case": "CS", "cases": "CS",
}

# Words that can make up a heading, a total or a price label but never a product.
_NON_PRODUCT_WORDS = {
    "item", "items", "qty", "quantity", "uom", "unit", "units", "size", "sizes",
    "description", "desc", "price", "prices", "extended", "amount", "total", "totals",
    "subtotal", "grand", "sum", "freight", "shipping", "tax", "taxes", "exempt", "cert",
    "on", "file", "page", "of", "no", "number", "line", "mark", "ref", "usd", "each",
    "ea", "per", "rate", "cost", "net", "terms", "notes", "note", "remarks", "lead", "time",
}


def tokens(text: str) -> list[str]:
    return _TOKEN.findall((text or "").lower())


def _qty_forms(qty) -> set[str]:
    if qty is None:
        return set()
    out = {str(qty)}
    if isinstance(qty, float) and qty.is_integer():
        out.add(str(int(qty)))
    if isinstance(qty, int):
        out.add(str(float(qty)))
    return {q.lower() for q in out}


def is_not_a_product(raw: str) -> bool:
    """True when every word in the row is a heading/total/price word."""
    toks = tokens(raw)
    words = [t for t in toks if any(c.isalpha() for c in t)]
    if not words:
        return True  # a row of nothing but numbers is a totals row, not an item
    return all(w in _NON_PRODUCT_WORDS for w in words)


def grounded(raw: str, haystack_tokens: set[str], threshold: float = 0.75) -> tuple[bool, float]:
    """Does this line's text actually occur in the document it claims to come from?

    Token coverage rather than substring, because a real line is often re-stitched
    from a table whose cells arrived on separate lines (a pasted spreadsheet puts
    qty, description and size on three rows).  Coverage still fails hard for a line
    lifted out of quoted history or invented wholesale.
    """
    toks = tokens(raw)
    if len(toks) < 2:
        return False, 0.0
    hit = sum(1 for t in toks if t in haystack_tokens)
    cov = hit / len(toks)
    return cov >= threshold, round(cov, 3)


def normalise_uom(uom, raw: str) -> str | None:
    if uom:
        u = str(uom).strip()
        if u.upper() in UOMS:
            return u.upper()
        if u.lower() in UOM_ALIASES:
            return UOM_ALIASES[u.lower()]
    # last resort: a unit word sitting next to the quantity in the raw text
    for t in tokens(raw):
        if t in UOM_ALIASES:
            return UOM_ALIASES[t]
    return None


def normalise_qty(qty):
    if qty is None:
        return None, None
    try:
        q = float(qty)
    except (TypeError, ValueError):
        return None, "qty was not a number"
    if q < 0 or q > 10_000_000:
        return None, f"qty {q} out of range"
    return (int(q) if q.is_integer() else q), None


def verify_lines(lines: list[dict], *, source: str, haystack: str | None, trace: list) -> list[dict]:
    """Normalise, ground and de-duplicate one document's lines."""
    hay = set(tokens(haystack)) if haystack is not None else None
    out: list[dict] = []
    seen: set[tuple] = set()

    for ln in lines or []:
        raw = (ln.get("raw") or "").strip()
        desc = (ln.get("description") or "").strip()
        if not raw or not desc:
            trace.append({"source": source, "raw": raw, "dropped": "empty raw or description"})
            continue

        if is_not_a_product(raw):
            trace.append({"source": source, "raw": raw, "dropped": "row is a heading/total/price label"})
            continue

        if hay is not None:
            ok, cov = grounded(raw, hay)
            if not ok:
                trace.append(
                    {"source": source, "raw": raw, "dropped": f"not found in {source} (token coverage {cov})"}
                )
                continue

        qty, qty_note = normalise_qty(ln.get("qty"))
        uom = normalise_uom(ln.get("uom"), raw)

        key = (tuple(tokens(raw)), qty, source)
        if key in seen:
            trace.append({"source": source, "raw": raw, "dropped": "duplicate of an earlier line"})
            continue
        seen.add(key)

        item = {
            "raw": raw,
            "description": desc,
            "qty": qty,
            "uom": uom,
            "source": source,
        }
        notes = []
        if qty_note:
            notes.append(qty_note)
        if qty is not None and hay is not None and not (_qty_forms(qty) & hay):
            notes.append("qty does not appear verbatim in the source")
        if ln.get("context_from"):
            item["_context_from"] = ln["context_from"]
        if notes:
            item["_notes"] = notes
        out.append(item)

    return out


def strip_internal(line: dict) -> dict:
    """Drop our own bookkeeping keys before the line goes into parsed.json."""
    return {k: v for k, v in line.items() if not k.startswith("_")}
