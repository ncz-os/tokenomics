"""Goose ingestion adapter — reads block/goose's SQLite session store.

Goose persists per-session usage AND cost in `~/.local/share/goose/sessions/sessions.db`
(`sessions` table). We read it non-invasively (read-only) and map each session to a
canonical tokenomics ledger row. Goose computes `accumulated_cost`, so cost is
host-authoritative; no pricing-catalog estimate is needed.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from tokenomics_core.ledger import LedgerEntry

DEFAULT_DB = Path.home() / ".local/share/goose/sessions/sessions.db"


def _model_from_config(blob: str | None) -> str:
    try:
        d = json.loads(blob or "{}")
    except Exception:
        return "unknown"
    return (
        d.get("model")
        or d.get("model_name")
        or (d.get("model_config") or {}).get("model")
        or "unknown"
    )


def _iso(ts: str) -> str:
    # Goose stores "YYYY-MM-DD HH:MM:SS" in UTC; normalize to ISO-8601 Z.
    try:
        return datetime.strptime(ts, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc).isoformat()
    except Exception:
        return ts


def iter_sessions(db_path: Path | str = DEFAULT_DB) -> Iterator[tuple[str, LedgerEntry]]:
    """Yield ``(session_id, LedgerEntry)`` for each Goose session carrying usage."""
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        rows = con.execute(
            "SELECT id, updated_at, provider_name, model_config_json, "
            "accumulated_input_tokens, accumulated_output_tokens, accumulated_cost "
            "FROM sessions"
        )
        for r in rows:
            tin = r["accumulated_input_tokens"] or 0
            tout = r["accumulated_output_tokens"] or 0
            cost = r["accumulated_cost"] or 0.0
            if tin == 0 and tout == 0 and cost == 0.0:
                continue  # no spend signal — skip (measurement gap, not a free call)
            yield str(r["id"]), LedgerEntry(
                ts_utc=_iso(r["updated_at"]),
                provider=r["provider_name"] or "unknown",
                model=_model_from_config(r["model_config_json"]),
                tokens_in=int(tin),
                tokens_out=int(tout),
                cost_usd=float(cost),
            )
    finally:
        con.close()
