# Ridgeline RFQ inbox reader

Reads an inbox of `.eml` files the way an inside sales rep does: decides what kind of email each
one is, and pulls out the line items being asked for — each one self-contained, with the
schedule, material or rating the customer wrote somewhere *other* than on that line.

```bash
python -m venv .venv && .venv/bin/pip install -e .
rfq parse --emails data/dev/emails --out parsed.json
```

**It runs with no API key.** Every model answer is recorded in `cache/` and committed, so the
dev inbox replays in under a second for $0.00 and produces byte-identical output.

## Results on the dev inbox

| | |
|---|---|
| silent losses · hallucinated lines · lost · phantom | **0 · 0 · 0 · 0** |
| classification | 24 / 24 |
| line items | precision 100% · recall 100% (210/210) |
| paired fields | uom · source · context carried 100% · qty 99.1% |
| `revises` / `duplicate_of` · `needs_review` | 100% / 100% · P 1.0 R 1.0 |
| cost | $0.2948 cold · $0.012 per email · **$0.0000** on re-run |

Those are measured by `scripts/score.py`, which implements the brief's metrics in the order it
says it grades them. They should be read with suspicion — see *Predictions* in `DECISIONS.md`.

## How it works

Six steps. Four are plain code, two call a model.

```
.eml → ① open the envelope   split the current message from the thread below it   code
     → ② read attachments    Excel · Word · CSV · PDF · photos → text or pictures  code
     → ③ what kind of email  one of seven — and a gate                            MODEL
     → ④ read each document  one call per document                                MODEL
     → ⑤ check every line    is it really in the document it claims?              code
     → ⑥ place in the inbox  duplicate · revision · needs review                   code
     → parsed.json
```

**The model only ever reads. Code decides what ships.** The model is good at *which words on
this page belong to this line* — no regex does that. It is never trusted with anything
checkable: whether a line exists, whether a unit is valid, whether an email is a duplicate,
whether a human needs to look. Those are code.

Step ③ is also a cost gate: four of the seven classes carry no line items, so those emails stop
there and cost $0.0007 instead of $0.03.

## The viewer

A local page that runs the same pipeline and shows every stage — including the exact prompt
sent to the model and the answer that came back.

```bash
rfq-ui                      # or: python ui/server.py
```

Opens `http://127.0.0.1:8765`, loopback only. **Drop `.eml` files on the page** — all at once or
a few at a time — then press **Process**. Dropping only reads the headers; nothing is sent
anywhere until you press it. Try `data/dev/emails/`; it replays from the committed cache, so it
returns in about a second and costs nothing.

The six stages are the columns and every email is a row, grouped by customer with revisions and
duplicates nested under the request they belong to. Each cell opens the thing it describes:

| column | what `view ›` opens |
|---|---|
| The email, as is | the message as it arrived, thread history greyed out beneath it |
| What code made of it | the record step ① built, the text kept, the text cut away, the header signals |
| Attachments | each file, how it routed, rendered images, the text the model is given — and a download |
| Classification | that model call in full: system prompt, user message, the JSON back |
| Model reads documents | one such card per document, with the strip images for a scan |
| Check every line | what was dropped and why, what could not be checked, what survived |
| Outcome | the final record, the lines a rep would quote, and what the email cost |

A **live calls** switch in the header ignores the recorded answers and calls the model for real.
It never writes to the cache, so experimenting cannot disturb the committed replay.

Three worth clicking: **E012** (a seven-line RFQ quoted under a status question, greyed out and
never shown to the model), **E011** (step ④ reads `skipped` — the gate, $0.0007), and **E009**
(a two-page phone scan, with the overlapping strips that are actually sent).

## Where everything is

| | |
|---|---|
| `RUNNING.md` | setup, the models, what a run costs, how to verify each property |
| `DECISIONS.md` | the design memo — the four sections the brief asks for |
| `MATCHER.md` | the optional catalog matcher, designed and not built |
| `AI_USAGE.md` · `ai-sessions/` | how the coding agent was directed, and the raw transcript |
| `rfq/` | the pipeline — one module per stage |
| `ui/` | the viewer. Reads the pipeline's output; never writes to it |
| `cache/` | recorded model answers. This is what makes the keyless replay work |
| `tests/` | four suites; three need no API key |
| `scripts/score.py` | the grader |

```bash
python scripts/score.py parsed.json data/dev/answer_key.json
python data/check_format.py parsed.json --emails data/dev/emails
python tests/test_documents.py      # and test_quoted_split, test_resilience
```

## A note on `data/` and `ASSESSMENT.md`

Those are the assessor's own material — the brief, the dev inbox and its answer key — kept in
the repo so the command and the scorer run straight from a clone. **This repository should stay
private for that reason.**
