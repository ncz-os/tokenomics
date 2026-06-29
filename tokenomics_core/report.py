"""Tokenomics report: local-first usage + chargeback rollups from the spend
ledger, priced by the pricing catalog.

Output field names are snake_case to match the companion Rust CLI's
``report --json`` and the TS ``@openclaw/tokenomics``, so the reports are
interchangeable.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timezone

from .ledger import Ledger
from .pricing import PricingCatalog
from .timeutil import day_key, hour_key, month_key, week_key

Gran = str  # "hour" | "day" | "week" | "month"


def parse_gran(s: str) -> Gran:
    return {
        "hour": "hour", "hourly": "hour",
        "week": "week", "weekly": "week",
        "month": "month", "monthly": "month",
    }.get(s.lower(), "day")


def _gran_key(gran: Gran, ts: datetime) -> str:
    return {"hour": hour_key, "day": day_key, "week": week_key, "month": month_key}[gran](ts)


@dataclass
class Bucket:
    key: str
    cost_usd: float = 0.0
    tokens_in: int = 0
    tokens_out: int = 0
    calls: int = 0


@dataclass
class RowByModel:
    model: str
    cost_usd: float = 0.0
    tokens: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    calls: int = 0
    billed: bool = False
    input_usd_per_mtok: float = 0.0
    output_usd_per_mtok: float = 0.0


@dataclass
class Report:
    period: str = ""
    since: str = ""
    until: str = ""
    days: int = 0
    bucket_gran: Gran = "day"
    buckets: list[Bucket] = field(default_factory=list)
    by_model: list[RowByModel] = field(default_factory=list)
    total_cost_usd: float = 0.0
    total_tokens: int = 0
    total_calls: int = 0
    free_tokens: int = 0
    billed_tokens: int = 0
    avoided_usd: float = 0.0
    counterfactual_usd: float = 0.0
    baseline_model: str = ""
    baseline_usd_per_mtok: float = 0.0

    def to_json(self) -> dict:
        return {
            "period": self.period,
            "since": self.since,
            "until": self.until,
            "days": self.days,
            "bucket_gran": self.bucket_gran,
            "buckets": [vars(b) for b in self.buckets],
            "by_model": [vars(m) for m in self.by_model],
            "total_cost_usd": self.total_cost_usd,
            "total_tokens": self.total_tokens,
            "total_calls": self.total_calls,
            "free_tokens": self.free_tokens,
            "billed_tokens": self.billed_tokens,
            "avoided_usd": self.avoided_usd,
            "counterfactual_usd": self.counterfactual_usd,
            "baseline_model": self.baseline_model,
            "baseline_usd_per_mtok": self.baseline_usd_per_mtok,
        }


def _parse_ts(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(timezone.utc)


def build_report(
    ledger: Ledger,
    pricing: PricingCatalog,
    since: datetime,
    until: datetime,
    gran: Gran = "day",
    period: str = "",
) -> Report:
    """Build a report over an explicit ``[since, until]`` window from the ledger,
    bucketing the time series at ``gran``. Cost is taken from each ledger entry;
    the baseline for the avoided-spend / counterfactual is derived from observed
    paid spend by default (an optional pricing catalog may override it)."""
    entries = ledger.entries_in(since, until)
    buckets: dict[str, Bucket] = {}
    models: dict[str, RowByModel] = {}

    rep = Report(
        period=period,
        since=day_key(since),
        until=day_key(until),
        days=max(1, math.ceil((until - since).total_seconds() / 86_400)),
        bucket_gran=gran,
    )

    for e in entries:
        cost = e.cost_usd
        billed = e.cost_usd > 0
        tok = e.tokens_in + e.tokens_out
        ts = _parse_ts(e.ts_utc)

        key = _gran_key(gran, ts)
        b = buckets.setdefault(key, Bucket(key=key))
        b.cost_usd += cost
        b.tokens_in += e.tokens_in
        b.tokens_out += e.tokens_out
        b.calls += e.calls

        m = models.setdefault(e.model, RowByModel(model=e.model))
        m.cost_usd += cost
        m.tokens += tok
        m.tokens_in += e.tokens_in
        m.tokens_out += e.tokens_out
        m.calls += e.calls
        m.billed = m.billed or billed

        rep.total_cost_usd += cost
        rep.total_tokens += tok
        rep.total_calls += e.calls
        if billed:
            rep.billed_tokens += tok
        else:
            rep.free_tokens += tok

    # Baseline for the avoided-spend / counterfactual headline. The authoritative
    # per-call cost already comes from the host (OpenClaw's own model.usage cost),
    # so by default the baseline is derived from observed spend -- the highest
    # effective $/Mtok among paid models actually used in the window -- rather than
    # from a separate price catalog. An optional pricing catalog may override it.
    baseline_per_mtok = pricing.baseline_usd_per_mtok
    baseline_model = pricing.baseline_model
    if baseline_per_mtok <= 0:
        best_rate = 0.0
        best_model = ""
        for row in models.values():
            if row.billed and row.tokens > 0:
                rate = (row.cost_usd / row.tokens) * 1_000_000
                if rate > best_rate:
                    best_rate = rate
                    best_model = row.model
        baseline_per_mtok = best_rate
        baseline_model = best_model
    rep.baseline_usd_per_mtok = baseline_per_mtok
    rep.baseline_model = baseline_model
    rep.avoided_usd = (rep.free_tokens / 1_000_000) * baseline_per_mtok
    rep.counterfactual_usd = (rep.total_tokens / 1_000_000) * baseline_per_mtok
    rep.buckets = sorted(buckets.values(), key=lambda x: x.key)

    for row in models.values():
        if row.billed:
            price = pricing.lookup(row.model)
            if price:
                row.input_usd_per_mtok = price.input_usd_per_mtok or 0.0
                row.output_usd_per_mtok = price.output_usd_per_mtok or 0.0
    rep.by_model = sorted(models.values(), key=lambda r: (-r.cost_usd, -r.tokens))

    return rep
