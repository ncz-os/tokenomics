"""Hermes ingestion adapter — NousResearch/hermes-agent.

Hermes persists per-session usage AND cost to its SQLite state store
``~/.hermes/state.db`` (`sessions` table): `input_tokens`, `output_tokens`,
cache/reasoning buckets, and `estimated_cost_usd` / `actual_cost_usd` with a
`cost_source` (host-authoritative). So — like Goose, unlike a patch — we read
that store non-invasively (read-only) and map each session to a canonical ledger
row. No upstream modification is required.

Cost precedence is host-neutral: `actual_cost_usd` → `estimated_cost_usd` →
pricing-catalog estimate → $0 (never invent).

A secondary surface (`usage_to_entry`) maps the live gateway status / `_get_usage`
snapshot dict, for callers polling a running Hermes instead of reading the store.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Optional

from tokenomics_core.ledger import LedgerEntry
from tokenomics_core.pricing import PricingCatalog

DEFAULT_DB = Path.home() / ".hermes" / "state.db"


def _epoch_to_iso(v) -> str:
    try:
        return datetime.fromtimestamp(float(v), tz=timezone.utc).isoformat()
    except Exception:
        return str(v)


def _provider_model(billing_provider, model, model_config_json) -> tuple[str, str]:
    cfg = {}
    if model_config_json:
        try:
            cfg = json.loads(model_config_json) or {}
        except Exception:
            cfg = {}
    provider = (billing_provider or cfg.get("provider") or "hermes")
    mdl = (model or cfg.get("model") or "unknown")
    # `billing_provider` may be a bare bucket like "custom" — prefer a routable
    # `model_config.provider` when present (matches Hermes's own resume logic).
    if str(provider) in ("custom", "") and cfg.get("provider"):
        provider = cfg["provider"]
    return str(provider), str(mdl)


def _cost(actual, estimated, model: str, tin: int, tout: int, pricing: Optional[PricingCatalog]) -> float:
    if isinstance(actual, (int, float)) and not isinstance(actual, bool) and actual > 0:
        return float(actual)
    if isinstance(estimated, (int, float)) and not isinstance(estimated, bool) and estimated > 0:
        return float(estimated)
    return pricing.cost(model, tin, tout) if pricing is not None else 0.0


def iter_sessions(
    db_path: Path | str = DEFAULT_DB,
    pricing: Optional[PricingCatalog] = None,
) -> Iterator[tuple[str, LedgerEntry]]:
    """Yield ``(session_id, LedgerEntry)`` for each Hermes session carrying spend."""
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        rows = con.execute(
            "SELECT id, COALESCE(ended_at, started_at) AS ts, model, model_config, "
            "billing_provider, input_tokens, output_tokens, "
            "estimated_cost_usd, actual_cost_usd "
            "FROM sessions"
        )
        for r in rows:
            tin = r["input_tokens"] or 0
            tout = r["output_tokens"] or 0
            cost = _cost(r["actual_cost_usd"], r["estimated_cost_usd"],
                         r["model"] or "", int(tin), int(tout), pricing)
            if tin == 0 and tout == 0 and cost == 0.0:
                continue  # no spend signal — skip (measurement gap, not a free call)
            provider, model = _provider_model(r["billing_provider"], r["model"], r["model_config"])
            yield str(r["id"]), LedgerEntry(
                ts_utc=_epoch_to_iso(r["ts"]),
                provider=provider,
                model=model,
                tokens_in=int(tin),
                tokens_out=int(tout),
                cost_usd=float(cost),
            )
    finally:
        con.close()


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
    """Map a live gateway status / `_get_usage` snapshot dict to a ledger row.

    Secondary surface for callers polling a running Hermes rather than reading
    `state.db`. Prefers the cache-inclusive `session_input/output_tokens`; falls
    back to `session_prompt/completion_tokens`. Cost comes from the catalog (the
    live snapshot carries tokens, not dollars)."""
    tin = _num(snap, "session_input_tokens", "session_prompt_tokens", "input", "prompt")
    tout = _num(snap, "session_output_tokens", "session_completion_tokens", "output", "completion")
    model = model or snap.get("model") or "unknown"
    provider = provider or snap.get("provider") or "hermes"
    ts = ts_utc or snap.get("session_start") or snap.get("ts_utc") or datetime.now(timezone.utc).isoformat()
    cost = pricing.cost(model, tin, tout) if pricing is not None else 0.0
    return LedgerEntry(ts_utc=ts, provider=provider, model=model, tokens_in=tin, tokens_out=tout, cost_usd=cost)
