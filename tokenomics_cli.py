"""tokenomics — unified ingestion engine CLI.

Thin wrapper over ``tokenomics_core.commands`` (the shared implementation also
used by the Hermes plugin).
  tokenomics ingest --host goose|hermes|osrun|freebuff --ledger <path> [--db <path>] [--pricing <p>] [--since <iso>]
  tokenomics report  --ledger <path> [--pricing pricing.json] [--days 30]
  tokenomics finops  --ledger <path> [--pricing pricing.json] [--days 30]
"""
from __future__ import annotations

import argparse
import json
import sys

from tokenomics_core import commands


def cmd_ingest(args) -> int:
    try:
        n = commands.ingest(args.host, args.ledger, db=getattr(args, "db", None),
                            pricing_path=getattr(args, "pricing", None),
                            since=getattr(args, "since", None))
    except ValueError as e:
        print(str(e), file=sys.stderr)
        return 2
    print(f"ingested {n} new {args.host} entr{'y' if n == 1 else 'ies'} -> {args.ledger}", file=sys.stderr)
    return 0


def cmd_report(args) -> int:
    print(commands.report(args.ledger, getattr(args, "pricing", None), args.days))
    return 0


def cmd_finops(args) -> int:
    print(json.dumps(commands.finops(args.ledger, getattr(args, "pricing", None), args.days),
                     indent=2, default=str))
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="tokenomics", description="Unified LLM-spend ingestion + FinOps")
    sub = ap.add_subparsers(dest="cmd", required=True)

    ing = sub.add_parser("ingest", help="pull a host's usage into the ledger")
    ing.add_argument("--host", required=True, choices=["goose", "hermes", "osrun", "freebuff"])
    ing.add_argument("--ledger", required=True)
    ing.add_argument("--db", help="source: goose sessions.db, hermes ~/.hermes/state.db, osrun JSONL ledger "
                     "(else $TOKENOMICS_OSRUN_LEDGER) or freebuff inbox dir (else $TOKENOMICS_FREEBUFF_INBOX)")
    ing.add_argument("--pricing", help="pricing.json — cost fallback when a host omits cost (hermes)")
    ing.add_argument("--since", help="osrun/freebuff: ignore records older than this ISO-8601 time")
    ing.set_defaults(fn=cmd_ingest)

    for name, fn in (("report", cmd_report), ("finops", cmd_finops)):
        p = sub.add_parser(name, help=f"{name} over the ledger")
        p.add_argument("--ledger", required=True)
        p.add_argument("--pricing", help="pricing.json (for avoided/counterfactual + cost estimate)")
        p.add_argument("--days", type=int, default=30)
        p.set_defaults(fn=fn)

    args = ap.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
