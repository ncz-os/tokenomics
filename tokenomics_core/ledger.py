"""Local spend ledger. Append-only JSONL, one record per model call. The on-disk
shape is intentionally identical to the companion Rust ledger and the TS
``@openclaw/tokenomics`` ledger (snake_case fields, RFC3339 ``ts_utc``) so a
ledger written by any tool is readable by the others.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from .timeutil import day_key, month_key, week_key, year_key


@dataclass
class LedgerEntry:
    ts_utc: str  # RFC3339 / ISO-8601 (UTC)
    provider: str
    model: str
    tokens_in: int
    tokens_out: int
    cost_usd: float
    #: Underlying calls this row represents (1 = per-call; >1 = rollup).
    calls: int = 1
    #: Set when the post-call free-policy guard flagged this spend.
    violation: Optional[str] = None

    def to_line(self) -> str:
        d = {
            "ts_utc": self.ts_utc,
            "provider": self.provider,
            "model": self.model,
            "tokens_in": self.tokens_in,
            "tokens_out": self.tokens_out,
            "cost_usd": self.cost_usd,
        }
        if self.calls != 1:
            d["calls"] = self.calls
        if self.violation:
            d["violation"] = self.violation
        return json.dumps(d)


Period = str  # "day" | "week" | "month" | "year"


def parse_period(s: str) -> Optional[Period]:
    return {
        "day": "day", "daily": "day",
        "week": "week", "weekly": "week",
        "month": "month", "monthly": "month",
        "year": "year", "yearly": "year",
    }.get(s.lower())


def _period_bucket(period: Period, ts: datetime) -> str:
    return {"day": day_key, "week": week_key, "month": month_key, "year": year_key}[period](ts)


@dataclass
class Rollup:
    cost_usd: float = 0.0
    tokens_in: int = 0
    tokens_out: int = 0
    calls: int = 0


def _parse_ts(s: str) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(timezone.utc)
    except (ValueError, AttributeError):
        return None


class Ledger:
    def __init__(self, path: str | os.PathLike):
        self.path = Path(path)

    def record(self, e: LedgerEntry) -> None:
        """Append one record as a single line (atomic under O_APPEND)."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(e.to_line() + "\n")

    def entries(
        self, on_malformed: Optional[Callable[[int, str], None]] = None
    ) -> list[LedgerEntry]:
        """All records. Per-line tolerant: a mangled/half-written line is skipped
        rather than aborting the rollup. ``on_malformed(line_no, raw_line)`` is
        invoked for every non-empty line that fails to parse, so dropped spend is
        observable instead of silently lost (mirrors the TS
        ``Ledger.entries({ onMalformed })`` and the Rust ``entries_observed``).
        """
        try:
            raw = self.path.read_text(encoding="utf-8")
        except OSError:
            return []
        out: list[LedgerEntry] = []
        for i, line in enumerate(raw.split("\n"), start=1):
            t = line.strip()
            if not t:
                continue
            try:
                j = json.loads(t)
            except ValueError:
                if on_malformed is not None:
                    on_malformed(i, line)
                continue
            if isinstance(j, dict) and isinstance(j.get("ts_utc"), str) and isinstance(j.get("model"), str):
                out.append(
                    LedgerEntry(
                        ts_utc=j["ts_utc"],
                        provider=str(j.get("provider", "")),
                        model=j["model"],
                        tokens_in=int(j.get("tokens_in", 0)),
                        tokens_out=int(j.get("tokens_out", 0)),
                        cost_usd=float(j.get("cost_usd", 0.0)),
                        calls=int(j.get("calls", 1)),
                        violation=j.get("violation"),
                    )
                )
            elif on_malformed is not None:
                on_malformed(i, line)
        return out

    def entries_in(
        self, since: Optional[datetime] = None, until: Optional[datetime] = None
    ) -> list[LedgerEntry]:
        """Records within an optional ``[since, until]`` window (inclusive)."""
        out = []
        for e in self.entries():
            t = _parse_ts(e.ts_utc)
            if t is None:
                continue
            if since is not None and t < since:
                continue
            if until is not None and t > until:
                continue
            out.append(e)
        return out

    def rollup(
        self, period: Period, since: Optional[datetime] = None, until: Optional[datetime] = None
    ) -> dict[str, Rollup]:
        """Spend rolled up by period bucket within an optional window (sorted key)."""
        out: dict[str, Rollup] = {}
        for e in self.entries_in(since, until):
            ts = _parse_ts(e.ts_utc)
            assert ts is not None
            key = _period_bucket(period, ts)
            r = out.setdefault(key, Rollup())
            r.cost_usd += e.cost_usd
            r.tokens_in += e.tokens_in
            r.tokens_out += e.tokens_out
            r.calls += e.calls
        return dict(sorted(out.items()))

    def by_model(
        self, since: Optional[datetime] = None, until: Optional[datetime] = None
    ) -> dict[str, Rollup]:
        """Spend grouped by model within an optional window."""
        out: dict[str, Rollup] = {}
        for e in self.entries_in(since, until):
            r = out.setdefault(e.model, Rollup())
            r.cost_usd += e.cost_usd
            r.tokens_in += e.tokens_in
            r.tokens_out += e.tokens_out
            r.calls += e.calls
        return dict(sorted(out.items()))
