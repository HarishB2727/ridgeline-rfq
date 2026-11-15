# AI usage

> Raw transcript in `ai-sessions/`. It contained the assessment key, pasted into the session
> before I thought about it, and a block of my own private test data; both are redacted, and
> `scripts/export_sessions.py` refuses to finish if either survives.

## Setup

**Claude Code** (CLI, Opus 5, 1M context) for all of it — one session, no subagents, no MCP
servers, no custom skills, **no `CLAUDE.md` or other instruction files**. Everything the agent
knew it read out of `ASSESSMENT.md`, `data/README.md`, the 24 `.eml` files and `answer_key.json`
at the start of the session.

That was deliberate. The failure mode I feared was an agent confidently writing a parser for
emails it had never opened, so the first instruction was to read the whole package and explain
the problem back to me before proposing anything, and the second was to design the architecture
in prose before writing a line of code. Both outputs are in the transcript ahead of the first
file.

The tooling that mattered is not an agent feature: `scripts/score.py`, written early,
implementing the brief's metrics in the order it says it grades them. After that existed, every
change was judged by a number rather than by whether the diff looked reasonable.

## How I worked

Deterministic layer first, verified with no model at all — all 24 emails ingested, every
attachment routed, the hidden sheet skipped, the encrypted PDF flagged — so that when model
answers arrived I knew the inputs were right. Then the scorer, then the model layer. From there
a loop: run, score, read the per-email diff, find the *cause* rather than patch the symptom, fix,
re-run. Five loops took dev from P 70.6% with 87 hallucinated lines to P/R 100% with none.

I reviewed every diff, under two rules. A fix goes in the layer that owns the decision — vague
quantities became code in `verify.py`, not another sentence in a prompt. And no fix may name a
string from the dev set: rules describe phenomena (hidden sheets, superseded revisions,
decorative images, bounce envelopes), because the held-out inbox has the same situations in
different words.

## Three moments

**It was wrong, and the scorer caught it — `0d8363a`.** Splitting tall scans into overlapping
strips fixed the quantity errors, so the agent added de-duplication by item number to drop rows
appearing in two strips. The next score run showed recall had *fallen*: E010 lost
`40 | EA | 1/2"-13 hex nut, zinc`. That screenshot's first column is the quantity, not an item
number, and two rows legitimately want 40 of something — "duplicate item number" had deleted a
real line. The fix checks whether the leading numbers form a contiguous index first. I would not
have caught this reading the diff; a net-positive change hid a regression inside it.

**I overruled it — twice, in opposite directions.** When the scan was misreading quantities the
obvious move was a bigger model. I made the agent measure instead: `gpt-5` scored 14/28 on that
page against `gpt-4.1`'s 13/28, while three overlapping strips scored 28/28. Row drift, not model
strength — the newer model would have cost more, risked the run on your key, and fixed nothing.
Later I was the one who assumed wrong: pinning temperature to 0 looks obviously right for
extraction, and on a workbook whose spec lives in its tab names context carried 0 of 54 lines at
temperature 0 and 54 of 54 at 1.0, twice each. Low temperature makes the model transcribe a row
instead of rewriting it self-contained — the entire task. Both measurements are code comments now.

**It saved me real time — `3c114a3`.** I asked which recent fixes a test would actually catch
regressing. Three were covered; two had been verified by hand in a scratch directory and that
evidence was gone. Writing the test took minutes and immediately found a case neither of us had
considered: a CSV named `.xlsx` fell through to generic text, losing the column structure that
separates a heading from a row.

**One more, because it is the test not the agent — `a0d8f40`.** The resilience inbox failed on
its first run: an email with an empty subject, empty body and one corrupt attachment comes back
`other`, and the format forbids `needs_review` on `other`, so it leaves with no lines and no
flag. A silent loss produced by following the spec — now a code override, and my disagreement
with the README.
