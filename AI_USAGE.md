# AI usage

> The raw transcript is in `ai-sessions/`. It contains the OpenAI key from the assessment
> email, pasted into the session before I thought about it; it is redacted there.

## Setup

**Claude Code** (CLI, Opus 5, 1M context) for all of it — one session, no subagents, no MCP
servers, no custom skills, no `CLAUDE.md`. Everything the agent knew about this problem it
read out of `ASSESSMENT.md`, `data/README.md`, the 24 `.eml` files and `answer_key.json` at
the start of the session.

That was deliberate. The failure mode I was worried about was an agent confidently writing a
parser for emails it had never opened, so the first instruction was to read the whole package
and explain the problem back to me before proposing anything, and the second was to design the
architecture in prose before writing a line of code. Both of those outputs are in the
transcript ahead of the first file.

The one piece of tooling that mattered is not an agent feature at all: `scripts/score.py`,
written early, implementing the brief's metrics in the order it says it grades them. After
that existed, every change was judged by a number instead of by whether the diff looked
reasonable.

## How I worked

Deterministic layer first and verified with no model at all — all 24 emails ingested, every
attachment routed, the hidden Excel sheet skipped, the encrypted PDF flagged — so that when
model answers started arriving I knew the inputs were right. Then the scorer, then the model
layer. From there it was a loop: run, score, read the per-email diff, find the *cause* rather
than patch the symptom, fix, re-run. Five of those loops took dev from P 70.6% / 87 hallucinated
lines to P/R 100% with none.

I reviewed every diff. Two rules I kept to: a fix goes in the layer that owns the decision
(vague quantities became a code rule in `verify.py`, not another sentence in a prompt), and no
fix may name a string from the dev set — rules are about phenomena (hidden sheets, superseded
revisions, decorative images, bounce envelopes) because the held-out inbox has the same
situations in different words.

## Three moments

**It was wrong, and the scorer caught it — `0d8363a`.** Splitting tall scans into overlapping
strips fixed the quantity errors, so the agent added de-duplication by item number to remove
rows appearing in two strips. The next score run showed line recall had *fallen*: E010 lost
`40 | EA | 1/2"-13 hex nut, zinc`. That screenshot's first column is the quantity, not an item
number, and two different rows legitimately want 40 of something — so "duplicate item number"
had deleted a real line. The fix was to check whether the leading numbers form a contiguous
index before trusting them. I would not have found this by reading the diff; a net-positive
change hid a regression inside it, and only a per-line comparison against the key surfaced it.
This is the strongest argument for having written the scorer first.

**I overruled it — twice, in opposite directions.** When the scanned RFQ was misreading
quantities the obvious move was a bigger model. I made the agent measure instead: `gpt-5`
scored 14/28 on that page against `gpt-4.1`'s 13/28, while three overlapping strips of the
same page scored 28/28. The failure was row drift over a tall table, not model strength — the
newer model would have cost more, risked the run on a grader's key, and fixed nothing. Later I
was the one who assumed wrong: pinning temperature to 0 looks obviously right for extraction,
and on a workbook whose specification lives in its tab names it carried context onto 0 of 54
lines at temperature 0 and 54 of 54 at 1.0, twice each. Low temperature makes the model
transcribe a row instead of rewriting it self-contained, which is the entire task. Both
measurements are in the code as comments, because the next person will have the same instinct.

**It saved me real time — `3c114a3`.** I asked which of the recent fixes a test would actually
catch regressing. Three were covered; two — hybrid PDFs and the spreadsheet row cap — had been
verified by hand in a scratch directory and that evidence was gone. Writing the test that
closed them took minutes and immediately found a case nobody had considered: a CSV named
`.xlsx` fell through to generic text and lost the column structure that separates a heading
from a row. It also pinned the exact rendering each dev attachment produces, which later caught
a cache-invalidating change the moment I made it.

**One more, because it is the test rather than the agent — `a0d8f40`.** The resilience inbox
failed on its first run, on a case neither of us had designed for: an email with an empty
subject, an empty body and one corrupt attachment comes back classified `other`, and the output
format forbids `needs_review` on `other`, so it leaves with no lines and no flag. A silent loss
produced by following the spec. That is now a code override, written up in `DECISIONS.md` as my
disagreement with the README.
