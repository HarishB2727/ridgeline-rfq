# Running it

## One setup step

```bash
python -m venv .venv && .venv/bin/pip install -e .
```

Windows: `py -m venv .venv && .venv\Scripts\pip install -e .`

Python 3.11+. Dependencies: `openai`, `openpyxl`, `python-docx`, `pymupdf`, `pillow` — all pure
wheels, no system packages, no poppler, no tesseract. Nothing is Linux-specific, so there is no
Dockerfile to need.

## The command

```bash
rfq parse --emails <dir> --out parsed.json
```

or, without installing:

```bash
python parse.py parse --emails <dir> --out parsed.json
```

`--out parsed.json` also writes `parsed.cost.json` beside it. Add `--traces traces/` for a
per-email record of what was found, what each document was judged to be, and every line that
was dropped and why.

Exit codes: `0` the run completed; `2` it completed but some emails could not reach a model
(those come back flagged); `1` it could not start.

## The API key

Read from `OPENAI_API_KEY`, or from a `.env` file beside this README containing
`OPENAI_API_KEY=sk-...`. 

**With no key at all the dev inbox still runs**, replayed from the recorded answers in
`cache/`, and produces byte-identical output. That is the property the brief asks for, and it
is tested below.

## Models

| stage | default | why |
|---|---|---|
| classify | `gpt-4.1-mini` | short input, one cheap call, gates everything downstream |
| extract (text) | `gpt-4.1` | the context-carrying judgement lives here |
| extract (pictures) | `gpt-4.1` | photos, scans, pasted screenshots |

Override per run with `--model-classify / --model-extract / --model-vision`, or the env vars
`RFQ_MODEL_CLASSIFY / RFQ_MODEL_EXTRACT / RFQ_MODEL_VISION`.

These are the models the committed cache was recorded with, so a keyless replay only works on
the defaults. If your key cannot reach one of them, `rfq/llm.py` falls through to the nearest
equivalent (`gpt-4o`, `gpt-4o-mini`) rather than failing the email, and records the answer
under the model that was *asked for* so later replays still hit.

The account this was built with also reached `gpt-5.x`. I did not default to it: it is not
universally available on other keys, I cannot report its price accurately, and on the one
measurement that mattered it was not better — see `DECISIONS.md`.

## What a run costs

Dev inbox, 24 emails, from an empty cache:

```
58 model calls · 96,424 prompt tokens · 19,891 completion tokens · $0.2882
$0.0120 per email
```

Spread is wide and deliberate. The four emails that carry no items — a vendor quote, a status
inquiry, a bounce, a newsletter — cost **$0.0007 each**, because classification runs first and
stops there. The expensive ones are the big documents: `$0.0380` for the 32-line two-page phone
scan, `$0.0332` for the 44-line PDF, `$0.0317` for the 50-line two-tab workbook.

A second run on the same inbox costs **$0.0000** and makes **0** model calls.

`parsed.cost.json` always reports both what this run spent and what the inbox would cost cold,
so a replay still tells you the real number. Prices come from the table in `rfq/llm.py` and are
printed with the report; override with `RFQ_PRICE_<MODEL>_IN` / `_OUT` if they drift.

## Verifying the four properties

```bash
# 1 + 2 — replayable with no key, and free the second time
rfq parse --emails data/dev/emails --out a.json      # cold: 58 calls, $0.2882
rfq parse --emails data/dev/emails --out b.json      # 0 calls, $0.0000
mv .env .env.off && rfq parse --emails data/dev/emails --out c.json && mv .env.off .env
shasum -a 256 a.json b.json c.json                   # all three identical

# 3 — one bad email never takes down the run
python tests/test_resilience.py

# 4 — cost
cat parsed.cost.json
```

## Scoring it yourself

```bash
python scripts/score.py parsed.json data/dev/answer_key.json
python scripts/score.py parsed.json data/dev/answer_key.json --detail E009
python data/check_format.py parsed.json --emails data/dev/emails
```

`scripts/score.py` implements the brief's metrics in the order it says it grades them, with the
spelling-equivalence table the README describes. Current dev numbers are in `DECISIONS.md`.

## Screen recording

<!-- TODO: paste the link here before sending. -->

## Layout

```
rfq/ingest.py      MIME, body text, current-vs-quoted split, header signals, fingerprints
rfq/documents.py   attachments -> uniform Documents; routing by magic bytes
rfq/classify.py    the seven classes, the code vetoes, the cost gate
rfq/extract.py     per-document extraction, page strips, revision merge
rfq/verify.py      groundedness, uom vocabulary, qty sanity, de-duplication
rfq/link.py        duplicates, revisions, the needs_review rules
rfq/llm.py         the single model call site: cache, cost ledger, retries, fallbacks
rfq/prompts.py     what the model is told
rfq/pipeline.py    phase A (parallel, per email) and phase B (ordered, per inbox)
cache/             recorded model answers — committed, this is what makes replay work
scripts/score.py   the grader, as described in data/README.md
tests/             the resilience inbox
```
