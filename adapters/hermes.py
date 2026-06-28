"""Hermes ingestion adapter — NousResearch/hermes-agent.

Hermes keeps usage in-process on the agent (`session_input_tokens`,
`session_output_tokens`, `session_prompt_tokens`, `session_completion_tokens`,
`session_total_tokens`, cache buckets, `session_api_calls`), surfaced via the
gateway status / `_get_usage` RPC and the cli status snapshot. It does NOT persist
a per-call ledger file — its "core ledger" is the hosted billing account. So we
ingest Hermes's *usage-snapshot* shape: either snapshots captured to disk or one
polled live from the gateway. Hermes reports tokens (not cost), so cost is
estimated from the pricing catalog.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Optional

from tokenomics_core.ledger import LedgerEntry
from tokenomics_core.pricing import PricingCatalog

DEFAULT_SNAPSHOT_DIR = Path.home() / ".hermes" / "sessions" / "usage"


def _num(snap: dict, *keys) -> int:
    for k in keys:
        v = snap.get(k)
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            return int(v)
    return 0


def usage_to_entry(
    snap: dict,
    pricing: Optional[PricingCatalog] = None,
    provider: str = "",
    model: str = "",
    ts_utc: str = "",
) -> LedgerEntry:
    """Map a Hermes usage/status snapshot dict to a canonical ledger row.

    Prefers the cache-inclusive `session_input/output_tokens`; falls back to the
    `session_prompt/completion_tokens` counters. Cost comes from the catalog when
    given (Hermes reports tokens, not dollars)."""
    tin = _num(snap, "session_input_tokens", "session_prompt_tokens", "input", "prompt")
    tout = _num(snap, "session_output_tokens", "session_completion_tokens", "output", "completion")
    model = model or snap.get("model") or "unknown"
    provider = provider or snap.get("provider") or "hermes"
    ts = ts_utc or snap.get("session_start") or snap.get("ts_utc") or datetime.now(timezone.utc).isoformat()
    cost = pricing.cost(model, tin, tout) if pricing is not None else 0.0
    return LedgerEntry(ts_utc=ts, provider=provider, model=model, tokens_in=tin, tokens_out=tout, cost_usd=cost)


def iter_sessions(source=None, pricing: Optional[PricingCatalog] = None) -> Iterator[tuple[str, LedgerEntry]]:
    """Yield ``(session_id, LedgerEntry)`` from captured Hermes usage snapshots.

    `source` is a directory of snapshot ``*.json`` files (each a status/`_get_usage`
    payload). A snapshot with no token/cost signal is skipped."""
    d = Path(source) if source else DEFAULT_SNAPSHOT_DIR
    if not d.exists():
        return
    for f in sorted(d.glob("*.json")):
        try:
            snap = json.loads(f.read_text())
        except Exception:
            continue
        sid = str(snap.get("session_id") or f.stem)
        entry = usage_to_entry(snap, pricing=pricing)
        if entry.tokens_in == 0 and entry.tokens_out == 0 and entry.cost_usd == 0.0:
            continue
        yield sid, entry
