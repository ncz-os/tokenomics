"""Shared command implementations — one ledger fed by host adapters, plus report
and finops. Lives in the unique ``tokenomics_core`` namespace so both the
``tokenomics`` CLI and the Hermes plugin import it without colliding with a
generic top-level module name (a real hazard when pip-installed alongside a host
like Hermes that ships its own ``cli``/``adapters`` modules).
"""
from __future__ import annotations

import dataclasses
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .finops import build_finops_report
from .ledger import Ledger
from .pricing import PricingCatalog
from .render import render_report
from .report import build_report


def _cursor(ledger: str, host: str) -> Path:
    return Path(f"{ledger}.{host}.ingested.json")


def _load_seen(p: Path) -> set:
    return set(json.loads(p.read_text())) if p.exists() else set()


def ingest(host: str, ledger: str, db=None, source=None, pricing_path=None) -> int:
    """Pull a host's usage into the ledger (dedup per session). Returns new-row count."""
    led = Ledger(ledger)
    cpath = _cursor(ledger, host)
    seen = _load_seen(cpath)
    if host == "goose":
        from .adapters.goose import DEFAULT_DB, iter_sessions
        pairs = iter_sessions(db or DEFAULT_DB)
    elif host == "hermes":
        from .adapters.hermes import DEFAULT_DB, iter_sessions
        pricing = PricingCatalog.load(pricing_path) if pricing_path else None
        pairs = iter_sessions(db or DEFAULT_DB, pricing=pricing)
    else:
        raise ValueError(f"unknown host: {host}")
    n = 0
    for sid, entry in pairs:
        if sid in seen:
            continue
        led.record(entry)
        seen.add(sid)
        n += 1
    cpath.write_text(json.dumps(sorted(seen)))
    return n


def _window(days: int):
    now = datetime.now(timezone.utc)
    return now - timedelta(days=days), now


def _pricing(path):
    return PricingCatalog.load(path) if path else PricingCatalog()


def report(ledger: str, pricing_path=None, days: int = 30) -> str:
    """Rendered spend report over the ledger (string)."""
    led = Ledger(ledger)
    since, until = _window(days)
    rep = build_report(led, _pricing(pricing_path), since, until, gran="day", period="tokenomics ingest")
    return render_report(rep)


def finops(ledger: str, pricing_path=None, days: int = 30) -> dict:
    """FinOps view over the ledger (dict ready for JSON)."""
    led = Ledger(ledger)
    since, until = _window(days)
    rep = build_finops_report(
        led, _pricing(pricing_path), since, until,
        generated=datetime.now(timezone.utc).isoformat(),
    )
    return dataclasses.asdict(rep)
