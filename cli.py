"""tokenomics — unified ingestion engine CLI.

One canonical ledger fed by per-host adapters; one report + finops across hosts.
  tokenomics ingest --host goose|hermes --ledger <path> [--db/--source <path>]
  tokenomics report  --ledger <path> [--pricing pricing.json] [--days 30]
  tokenomics finops  --ledger <path> [--pricing pricing.json] [--days 30]
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from tokenomics_core.ledger import Ledger
from tokenomics_core.pricing import PricingCatalog
from tokenomics_core.report import build_report
from tokenomics_core.render import render_report
from tokenomics_core.finops import build_finops_report


def _cursor(ledger: str, host: str) -> Path:
    return Path(f"{ledger}.{host}.ingested.json")


def _load_seen(p: Path) -> set[str]:
    return set(json.loads(p.read_text())) if p.exists() else set()


def cmd_ingest(args) -> int:
    led = Ledger(args.ledger)
    cpath = _cursor(args.ledger, args.host)
    seen = _load_seen(cpath)
    n = 0
    if args.host == "goose":
        from adapters.goose import DEFAULT_DB, iter_sessions
        pairs = iter_sessions(args.db or DEFAULT_DB)
    elif args.host == "hermes":
        from adapters.hermes import iter_sessions
        pricing = PricingCatalog.load(args.pricing) if getattr(args, "pricing", None) else None
        pairs = iter_sessions(args.source, pricing=pricing)
    else:
        print(f"unknown host: {args.host}", file=sys.stderr)
        return 2
    for sid, entry in pairs:
        if sid in seen:
            continue
        led.record(entry)
        seen.add(sid)
        n += 1
    cpath.write_text(json.dumps(sorted(seen)))
    print(f"ingested {n} new {args.host} entr{'y' if n == 1 else 'ies'} -> {args.ledger}", file=sys.stderr)
    return 0


def _window(days: int):
    now = datetime.now(timezone.utc)
    return now - timedelta(days=days), now


def _pricing(path):
    return PricingCatalog.load(path) if path else PricingCatalog()


def cmd_report(args) -> int:
    led = Ledger(args.ledger)
    since, until = _window(args.days)
    rep = build_report(led, _pricing(args.pricing), since, until, gran="day", period="tokenomics ingest")
    print(render_report(rep))
    return 0


def cmd_finops(args) -> int:
    led = Ledger(args.ledger)
    since, until = _window(args.days)
    rep = build_finops_report(led, _pricing(args.pricing), since, until, generated=datetime.now(timezone.utc).isoformat())
    print(json.dumps(dataclasses.asdict(rep), indent=2, default=str))
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="tokenomics", description="Unified LLM-spend ingestion + FinOps")
    sub = ap.add_subparsers(dest="cmd", required=True)

    ing = sub.add_parser("ingest", help="pull a host's usage into the ledger")
    ing.add_argument("--host", required=True, choices=["goose", "hermes"])
    ing.add_argument("--ledger", required=True)
    ing.add_argument("--db", help="goose sessions.db (default ~/.local/share/goose/sessions/sessions.db)")
    ing.add_argument("--source", help="hermes usage-snapshot dir (default ~/.hermes/sessions/usage)")
    ing.add_argument("--pricing", help="pricing.json — estimate cost for token-only hosts (hermes)")
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
