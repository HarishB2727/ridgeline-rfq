"""The command.

    parse --emails <dir> --out parsed.json

Exit codes: 0 the run completed (flagged emails are a normal outcome, not a
failure), 2 the run completed but some emails could not reach a model, 1 the
run could not start at all.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from .pipeline import DEFAULT_CLASSIFY_MODEL, DEFAULT_EXTRACT_MODEL, DEFAULT_VISION_MODEL, run

REPO = Path(__file__).resolve().parent.parent


def default_cache() -> Path:
    """Where the recorded answers live.

    Installed with `pip install -e .` the package sits in the repo and REPO is right.
    Installed without -e it sits in site-packages, where there is no cache — so the
    working directory wins when it has one.  Getting this wrong silently turns a
    keyless replay into 'no recorded answer for this input' on every email.
    """
    for candidate in (Path.cwd() / "cache", REPO / "cache"):
        if candidate.is_dir():
            return candidate
    return Path.cwd() / "cache"


def _load_dotenv() -> None:
    """Read OPENAI_API_KEY from a .env beside the repo if the environment has none."""
    if os.getenv("OPENAI_API_KEY"):
        return
    for env in (Path.cwd() / ".env", REPO / ".env"):
        if not env.is_file():
            continue
        for line in env.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip("'\""))
        return


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="rfq", description=__doc__)
    sub = ap.add_subparsers(dest="command", required=True)

    p = sub.add_parser("parse", help="read an inbox of .eml files into parsed.json")
    p.add_argument("--emails", type=Path, required=True, help="directory of .eml files")
    p.add_argument("--out", type=Path, required=True, help="where to write parsed.json")
    p.add_argument("--cache", type=Path, default=None, help="recorded model answers (default: ./cache, else the repo's)")
    p.add_argument("--cost-report", type=Path, default=None, help="where to write the cost summary")
    p.add_argument("--traces", type=Path, default=None, help="directory for per-email traces")
    p.add_argument("--concurrency", type=int, default=6)
    p.add_argument("--refresh", action="store_true", help="ignore the cache and call the model again")
    p.add_argument("--no-cache-write", action="store_true", help="do not record new answers")
    p.add_argument("--model-classify", default=DEFAULT_CLASSIFY_MODEL)
    p.add_argument("--model-extract", default=DEFAULT_EXTRACT_MODEL)
    p.add_argument("--model-vision", default=DEFAULT_VISION_MODEL)
    p.add_argument("--quiet", action="store_true")
    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command != "parse":  # pragma: no cover
        return 1

    _load_dotenv()
    if not args.emails.is_dir():
        print(f"--emails: {args.emails} is not a directory", file=sys.stderr)
        return 1

    parsed, cost, results = run(
        args.emails,
        cache_dir=args.cache or default_cache(),
        api_key=os.getenv("OPENAI_API_KEY"),
        models={
            "classify": args.model_classify,
            "extract": args.model_extract,
            "vision": args.model_vision,
        },
        concurrency=max(1, args.concurrency),
        refresh=args.refresh,
        write_cache=not args.no_cache_write,
    )

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(parsed, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    report = args.cost_report or args.out.with_name(args.out.stem + ".cost.json")
    report.write_text(json.dumps(cost, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    if args.traces:
        args.traces.mkdir(parents=True, exist_ok=True)
        for r in results:
            (args.traces / f"{r.id}.json").write_text(
                json.dumps(r.trace(), indent=2, ensure_ascii=False, default=str) + "\n", encoding="utf-8"
            )

    degraded = [r.id for r in results if r.errors]
    if not args.quiet:
        flagged = [r.id for r in results if r.needs_review]
        lines = sum(len(v["line_items"]) for v in parsed.values())
        print(f"{len(parsed)} emails -> {args.out}  ({lines} line items)")
        cold = cost["cold_run"]
        print(
            f"  model calls {cost['model_calls']}, cache hits {cost['cache_hits']}, "
            f"${cost['cost_usd']:.4f} spent this run"
            + ("  [replayed from cache, no API key]" if cost["offline_replay"] else "")
        )
        print(
            f"  cold run would be {cold['prompt_tokens']:,} in / {cold['completion_tokens']:,} out tokens, "
            f"${cold['cost_usd']:.4f} (${cold['cost_per_email_usd']:.5f}/email)"
        )
        print(f"  needs_review: {len(flagged)}" + (f" ({', '.join(flagged)})" if flagged else ""))
        if degraded:
            print(f"  degraded (could not reach a model): {', '.join(degraded)}", file=sys.stderr)
        print(f"  cost report -> {report}")
    return 2 if degraded else 0
