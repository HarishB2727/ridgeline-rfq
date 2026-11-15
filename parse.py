#!/usr/bin/env python3
"""Run without installing:  python parse.py parse --emails data/dev/emails --out parsed.json"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from rfq.cli import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
