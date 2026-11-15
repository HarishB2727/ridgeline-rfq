# The matcher — designed, not built

Each extracted line has to become a catalog part, or a decision that Ridgeline doesn't stock
it. I did not build this; here is how I would, and why it is shaped the way it is.

## The shape the catalog hands you

`catalog.csv` is not a list of descriptions — it is already a structured table:
`category, product, size, spec, material, ends, extra, uom`, with 937 rows over 7 categories
and a closed vocabulary in every column (12 `ends` values, 22 `spec` values, `extra` is one of
nine). `description` is just an ERP rendering of those columns.

That settles the approach. **This is not a text-similarity problem.** Fuzzy-matching a
customer's sentence against an abbreviated ERP description is exactly how you quote the wrong
part, because the nearest string is routinely one attribute away from the right one. The job is
to parse the customer's free text into the *same schema the catalog already uses*, then compare
attributes exactly.

## The decision

1. **Normalise the line into catalog attributes.** From `description` (which the parser has
   already made self-contained) produce `{category, product, size, spec, material, ends, extra}`,
   using each column's own vocabulary — `Schedule 40`→`SCH 40`, `150 lb`/`Class 150`→`150#`,
   `butt weld`→`BW`, `threaded`/`NPT`→`THD`, `seamless`→`SMLS`, `1 1/2"`→`1-1/2`. Leave an
   attribute **absent** when the customer didn't state it. Absent is not a wildcard-with-a-guess;
   it is information about what they didn't pin down.

2. **Generate candidates** by exact index on `(product, size)` — a dictionary lookup, no
   scoring. `2" 90 elbow` gives 14 rows. Product names come from a synonym map onto the
   catalog's own `product` column (`ell`→`90 elbow`, `SPW`→`spiral wound gasket`, `WN`→`weld
   neck flange`, `T&C`→ends, not product).

3. **Filter by every attribute the customer DID state.** A stated attribute must agree exactly.
   Nothing is rounded, substituted or taken as "close enough".

4. **Decide by how many rows survive:**
   - exactly one → `matched`, that sku
   - more than one → `ambiguous`, `sku: null`, survivors in `candidates`
   - none → `not_in_catalog`, `sku: null`

The whole design is in step 4's symmetry: **silence widens the candidate set; it never narrows
it.** `1" WN 150# RF` names no material, and the catalog holds exactly one 1" 150# RF weld neck
(`RL-26367`, A105) — one survivor, so it matches, correctly, without anyone guessing a material.
Ask for `2" ball valve, threaded` and five rows survive on port and WOG rating and body
material; that is genuinely a question for the customer, not a coin flip.

## Why not-in-catalog has to be a real answer

Every one of the 16 non-matched lines in the dev key is one attribute away from something we
stock, which is exactly what makes substitution tempting and wrong:

| the customer asked for | what we stock | if you substituted |
|---|---|---|
| 14" weld neck flange, **150#** | 14" weld neck only in **300#** | wrong pressure class |
| 2" spiral wound, **316**/graphite, 300# | same size and class in **304** | wrong gasket material |
| 2" ball valve, **bronze, 125#** | 2" ball only in 304/316 SS at WOG ratings | wrong material and rating |
| **5/8" x 3-1/4"** B7 stud | 5/8" x 3" and 5/8" x **3-1/2"** | bolt that doesn't fit the flange |
| **eccentric** reducer 6" x 4" | **concentric** reducer only | wrong geometry for the line |
| **half** coupling 2" | full coupling only | wrong part |
| Y-strainer, hex head plug | not carried at all | — |

Three of those (material, class, length) would pass any string-similarity threshold loose
enough to be useful. So: no fallback, no "nearest part", no confidence score that lets a near
miss through. A `not_in_catalog` costs a buyer a phone call; a confident wrong sku costs a
rebuild.

## Where the model helps, and what it must never do

The model does **step 1 only** — reading a customer's prose into the attribute schema, which is
the same context-carrying judgement the parser already does well. It returns attributes, with
each one marked stated or absent.

The model never sees the catalog, never ranks candidates, and never names a sku. Steps 2–4 are
a dictionary lookup and a set intersection: deterministic, auditable, instantly re-runnable
when the catalog changes, and incapable of inventing `RL-31415`. If a model picks the part, a
wrong pick is unexplainable and unreproducible; if code picks it, `match_reason` writes itself —
*"2" 3000# A105 THD 90 elbow: one part fits"*, or *"14" weld neck 150#: we stock 14" weld neck
only in 300#"*.

One place I would let the model back in, read-only: when zero rows survive, ask it to name
*which attribute* caused it, so the reason line is useful to a rep. Its answer goes in the text,
never in the decision.

## How I'd measure it

The brief grades confident wrong matches first, so that is the headline: **a line where we named
a sku and the key says a different sku, `not_in_catalog`, or `ambiguous`.** Target zero; it is
the only matcher error that reaches a customer as a wrong quote. Then `not_in_catalog` recall
(of the 16 the key marks, how many did we decline to match), then plain accuracy, and
separately the `ambiguous` split — because the failure mode of a cautious matcher is flooding
reps with questions, and `ambiguous` on something a rep would call obvious is a real cost even
though the brief doesn't score it.

`scripts/score.py` already pairs our lines to the key's, so adding the matcher's three numbers
is a small extension of the same loop.

Two things I would watch on held-out data. Dev contains **no `ambiguous` lines at all** —
194 matched, 16 not-in-catalog — so my ambiguity rule is entirely untested and is the part most
likely to be wrong. And matching inherits every parser error upstream: a description missing its
schedule because a heading didn't carry becomes an ambiguity or a miss here, so matcher accuracy
is bounded above by the "context carried" number in `DECISIONS.md` and should be read alongside
it, never on its own.
