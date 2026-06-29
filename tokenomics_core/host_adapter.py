"""Host-neutral ingestion seam.

Tokenomics is not tied to any one coding tool. Every host - pi, Claude Code,
Codex, Cursor, OpenClaw - captures per-call usage in its own way. Each one
normalizes that into a :class:`UsageEvent` and calls :func:`ingest`, which writes
a single ledger row. The report/render layers then work identically regardless
of host.

Cost precedence: an explicit ``cost_usd`` from the host wins. Otherwise, if the
model is classified free, cost is $0; else the pricing catalog estimates it.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Optional

from .ledger import Ledger, LedgerEntry
from .pricing import PricingCatalog


@dataclass
class UsageEvent:
    provider: str
    model: str
    tokens_in: int
    tokens_out: int
    cost_usd: Optional[float] = None
    ts_utc: Optional[str] = None
    host: Optional[str] = None
    #: Host-asserted free classification; overrides the catalog/predicate.
    free: Optional[bool] = None
    violation: Optional[str] = None


@dataclass
class IngestOptions:
    pricing: Optional[PricingCatalog] = None
    #: Classify a model as free ($0). Beats the catalog; loses to ``event.free``.
    is_free: Optional[Callable[[str, str], bool]] = None
    now: Optional[Callable[[], datetime]] = None


def resolve_cost(event: UsageEvent, opts: IngestOptions = IngestOptions()) -> float:
    if isinstance(event.cost_usd, (int, float)) and math.isfinite(event.cost_usd):
        return float(event.cost_usd)
    free = event.free
    if free is None and opts.is_free is not None:
        free = opts.is_free(event.model, event.provider)
    if free:
        return 0.0
    return opts.pricing.cost(event.model, event.tokens_in, event.tokens_out) if opts.pricing else 0.0


def to_ledger_entry(event: UsageEvent, opts: IngestOptions = IngestOptions()) -> LedgerEntry:
    now = opts.now or (lambda: datetime.now(timezone.utc))
    ts = event.ts_utc or now().isoformat()
    return LedgerEntry(
        ts_utc=ts,
        provider=event.provider,
        model=event.model,
        tokens_in=event.tokens_in,
        tokens_out=event.tokens_out,
        cost_usd=resolve_cost(event, opts),
        violation=event.violation,
    )


def ingest(ledger: Ledger, event: UsageEvent, opts: IngestOptions = IngestOptions()) -> LedgerEntry:
    """Append one usage event to the ledger. Returns the row written."""
    entry = to_ledger_entry(event, opts)
    ledger.record(entry)
    return entry


class HostAdapter:
    """Binds a ledger + ingestion policy so a host integration only has to
    translate its native event into a :class:`UsageEvent` and call ``track``."""

    def __init__(self, ledger_path: str, host: str, opts: IngestOptions = IngestOptions()):
        self.ledger = Ledger(ledger_path)
        self._host = host
        self._opts = opts

    def track(self, event: UsageEvent) -> LedgerEntry:
        event.host = self._host
        return ingest(self.ledger, event, self._opts)
