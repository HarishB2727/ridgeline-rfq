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
rows that appear in two strips. The next score run showed line recall had *fallen*: E010 lost
`40 | EA | 1/2"-13 hex nut, zinc`. That screenshot's first column is the quantity, not an item
number, and two different rows legitimately want 40 of something — so "duplicate item number"
had deleted a real line. The fix was to check whether the leading numbers form a contiguous
index before trusting them. I would not have found this by reading the diff; a net-positive
change hid a regression inside it, and only a per-line comparison against the key surfaced it.
This is the single strongest argument for having written the scorer first.

**I overruled it — `928bd5c` / `RUNNING.md`.** The key reached `gpt-5`, `gpt-5.4`, `gpt-5.5`
and more, and the obvious move was to default to the newest thing available. I said no, for a
reason that has nothing to do with quality: *they* run the held-out inbox with *their* key, and
if it cannot reach the model I named, the run dies. Defaults are pinned to the `gpt-4.1` family
— broadly available, and prices I can actually state in a cost report — with a fallback chain
for keys that cannot reach even those. The agent then tested the assumption I had implicitly
made, which was the better half of the exchange: on the scanned table that was failing,
`gpt-5` scored 14/28 and `gpt-4.1` scored 13/28. The newer model would have cost more, risked
the run, and fixed nothing.

**It saved me real time — `0d8363a`.** Ten quantities were wrong on E009's phone scan and I
would have spent an hour on image preprocessing — deskewing, upscaling, contrast. Instead the
agent proposed measuring four strategies against the answer key before changing anything:
full page, three overlapping strips, and two other models. That experiment took about four
minutes and returned an unambiguous answer (13/28 vs 28/28 vs 14/28) which both identified the
real mechanism — row drift over a tall table, not recognition — and ruled out the two
approaches I would have tried first.

**One more worth recording, because it is the test rather than the agent.** The resilience
inbox in `tests/test_resilience.py` failed on its first run, on a case neither of us had
designed for: an email with an empty subject, an empty body and one corrupt attachment comes
back classified `other`, and the output format forbids `needs_review` on `other`, so it leaves
with no lines and no flag. A silent loss produced by following the spec. That is now a code
override and it is written up in `DECISIONS.md` as my disagreement with the README. The agent
wrote the test; the test found the bug; neither the agent nor I had reasoned our way to it.
