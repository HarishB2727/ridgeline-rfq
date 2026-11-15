#!/usr/bin/env python3
"""The current message vs the thread beneath it, across mail clients.

This split is the single biggest guard against hallucinated lines: an RFQ quoted
under a one-line status question must never reach the extractor. It is pure code
and costs nothing to test, so it is tested exhaustively rather than on the four
client formats the dev inbox happens to contain.

Each case is a real request of two lines followed by history that must not be
read. A failure here does not crash anything — it quietly turns someone else's
old order into today's quote.

    python tests/test_quoted_split.py
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from rfq.ingest import split_quoted  # noqa: E402

CURRENT = "Hi,\n\nPlease quote 10 ea - 3\" 150# RF weld neck flange, A105.\n\nThanks,\nDale"
SECRET = "99 ea - 6\" 300# RF blind flange"  # must never appear in the current part

CASES: dict[str, str] = {
    "gmail desktop": f"""{CURRENT}

On Tue, Oct 6, 2026 at 8:47 AM Terrence Bailey <tbailey@x.example> wrote:
> Hi,
> Please quote {SECRET}
""",
    "gmail mobile (wrapped)": f"""{CURRENT}

On Tue, 6 Oct 2026, 08:47 Terrence Bailey, <tbailey@x.example>
wrote:
> Please quote {SECRET}
""",
    "apple mail": f"""{CURRENT}

On 6 Oct 2026, at 08:47, Terrence Bailey <tbailey@x.example> wrote:

Please quote {SECRET}
""",
    "outlook desktop": f"""{CURRENT}

-----Original Message-----
From: Terrence Bailey <tbailey@x.example>
Sent: Tuesday, October 6, 2026 8:47 AM
To: Ridgeline RFQ Desk
Subject: RFQ

Please quote {SECRET}
""",
    "outlook web (rule line)": f"""{CURRENT}

________________________________
From: Terrence Bailey <tbailey@x.example>
Sent: Tuesday, October 6, 2026 8:47 AM
Subject: RFQ

Please quote {SECRET}
""",
    "outlook bare header block": f"""{CURRENT}

From: Terrence Bailey <tbailey@x.example>
Sent: Tuesday, October 6, 2026 8:47 AM
To: Ridgeline RFQ Desk
Subject: RFQ

Please quote {SECRET}
""",
    "gmail forward": f"""{CURRENT}

---------- Forwarded message ---------
From: Terrence Bailey <tbailey@x.example>
Date: Tue, 6 Oct 2026 at 08:47
Subject: RFQ

Please quote {SECRET}
""",
    "apple forward": f"""{CURRENT}

Begin forwarded message:

From: Terrence Bailey <tbailey@x.example>
Subject: RFQ
Date: 6 October 2026 at 08:47

Please quote {SECRET}
""",
    "bare quoting, no intro": f"""{CURRENT}

> Please quote {SECRET}
""",
    "lotus/notes style": f"""{CURRENT}

----- Forwarded by Marcus Doyle on 10/06/2026 08:47 AM -----

Please quote {SECRET}
""",
    "german outlook": f"""{CURRENT}

Von: Terrence Bailey <tbailey@x.example>
Gesendet: Dienstag, 6. Oktober 2026 08:47
An: Ridgeline RFQ Desk
Betreff: Anfrage

Please quote {SECRET}
""",
    "french outlook": f"""{CURRENT}

De : Terrence Bailey <tbailey@x.example>
Envoyé : mardi 6 octobre 2026 08:47
À : Ridgeline RFQ Desk
Objet : Demande

Please quote {SECRET}
""",
    "spanish outlook": f"""{CURRENT}

De: Terrence Bailey <tbailey@x.example>
Enviado: martes, 6 de octubre de 2026 8:47
Para: Ridgeline RFQ Desk
Asunto: Solicitud

Please quote {SECRET}
""",
}

# Must NOT be split: these are ordinary requests that merely look header-ish.
NEGATIVE: dict[str, str] = {
    "no history at all": CURRENT,
    "'from' used in prose": "Hi,\n\nHere is the list from the yard foreman.\n\n10 ea - 3\" flange\n\nDale",
    "a line that starts with On": "Hi,\n\nOn the skid we need:\n\n10 ea - 3\" flange\n4 ea - 2\" tee\n\nDale",
    "ship-to block with To:": "Hi,\n\nPlease quote 10 ea - 3\" flange.\n\nTo: Plant 2 dock\nAttn: receiving\n\nDale",
}


def main() -> int:
    width = max(len(k) for k in list(CASES) + list(NEGATIVE)) + 2
    failures = []

    print("history must be split off:")
    for name, text in CASES.items():
        current, quoted = split_quoted(text)
        leaked = SECRET in current
        found = bool(quoted.strip())
        kept = "Please quote 10 ea" in current
        ok = found and not leaked and kept
        print(f"  {name:<{width}} {'ok' if ok else 'FAIL'}"
              f"   current={len(current):>4}ch quoted={len(quoted):>4}ch")
        if not ok:
            failures.append(
                f"{name}: " + ", ".join(
                    x for x in (
                        "history not detected" if not found else "",
                        "OLD ORDER LEAKED INTO THE CURRENT MESSAGE" if leaked else "",
                        "the real request was lost" if not kept else "",
                    ) if x
                )
            )

    print("\nordinary requests must NOT be split:")
    for name, text in NEGATIVE.items():
        current, quoted = split_quoted(text)
        ok = not quoted.strip() and "10 ea - 3\"" in current
        print(f"  {name:<{width}} {'ok' if ok else 'FAIL'}   current={len(current):>4}ch quoted={len(quoted):>4}ch")
        if not ok:
            failures.append(f"{name}: a plain request was cut in half")

    if failures:
        print("\nFAILURES:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("\nevery client format splits correctly")
    return 0


if __name__ == "__main__":
    sys.exit(main())
