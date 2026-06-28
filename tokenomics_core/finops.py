"""FinOps observability layer — read-only, non-enforcing (host-neutral core).

Python port of `@openclaw/tokenomics/finops.ts` / `zoder-engine`'s `finops.rs`:
allocation, realized-rate, cache-discount savings, cheapest-equivalent advisor,
and burn forecasting over the shared append-only ledger. Every output is data
for a human or tool to act on; nothing here enforces a budget or swaps a model.

Optional FinOps tags (caller/task/tier/cache_hit_ratio) are read off the ledger
entry when a host attaches them; absent fields simply group as ``__untagged__``.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from .ledger import Ledger, LedgerEntry
from .pricing import ModelPrice, PricingCatalog


def _in_window(ts_utc: str, since: Optional[datetime], until: Optional[datetime]) -> bool:
    if since is None and until is None:
        return True
    t = datetime.fromisoformat(ts_utc.replace("Z", "+00:00"))
    if since is not None and t < since:
        return False
    if until is not None and t > until:
        return False
    return True


def _tag(e: LedgerEntry, dim: str) -> str:
    raw = getattr(e, dim, None)
    return "__untagged__" if raw in (None, "") else str(raw)


@dataclass
class SpendGroup:
    key: str
    cost_usd: float = 0.0
    tokens_in: int = 0
    tokens_out: int = 0
    calls: int = 0


def spend_by_dimension(ledger: Ledger, dim: str, since=None, until=None) -> list[SpendGroup]:
    """Allocation: spend grouped by a single dimension (provider/model/caller/task)."""
    acc: dict[str, SpendGroup] = {}
    for e in ledger.entries():
        if not _in_window(e.ts_utc, since, until):
            continue
        key = _tag(e, dim)
        g = acc.setdefault(key, SpendGroup(key=key))
        g.cost_usd += e.cost_usd
        g.tokens_in += e.tokens_in
        g.tokens_out += e.tokens_out
        g.calls += 1
    return sorted(acc.values(), key=lambda g: g.cost_usd, reverse=True)


@dataclass
class ModelRealized:
    model: str
    cost_usd: float = 0.0
    tokens: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    calls: int = 0
    realized_usd_per_mtok: float = float("nan")


def realized_rate_by_model(ledger: Ledger, since=None, until=None) -> list[ModelRealized]:
    """Effective $/1M tok from actual ledger spend (can differ from catalog)."""
    acc: dict[str, ModelRealized] = {}
    for e in ledger.entries():
        if not _in_window(e.ts_utc, since, until):
            continue
        r = acc.setdefault(e.model, ModelRealized(model=e.model))
        r.cost_usd += e.cost_usd
        r.tokens_in += e.tokens_in
        r.tokens_out += e.tokens_out
        r.tokens += e.tokens_in + e.tokens_out
        r.calls += 1
    for r in acc.values():
        r.realized_usd_per_mtok = (r.cost_usd / r.tokens) * 1_000_000 if r.tokens > 0 else float("nan")
    return sorted(acc.values(), key=lambda r: r.cost_usd, reverse=True)


def _effective_rate(p: ModelPrice) -> float:
    # 70/30 input/output heuristic when only component rates are known (report-only).
    i = p.input_usd_per_mtok or 0.0
    o = p.output_usd_per_mtok or 0.0
    if i or o:
        return 0.7 * i + 0.3 * o
    return p.usd_per_mtok or 0.0


@dataclass
class AdvisorRow:
    paid_model: str
    paid_cost_usd: float
    calls: int
    tokens: int
    cheapest_alt_model: str
    cheapest_alt_usd_per_mtok: float
    cheapest_alt_estimated_cost_usd: float
    potential_savings_usd: float
    potential_savings_ratio: float


def cheapest_equivalent_advisor(ledger: Ledger, pricing: PricingCatalog, since=None, until=None) -> list[AdvisorRow]:
    """Report-only: for each paid model, the cheapest catalog alternative for the same token volume."""
    paid: dict[str, dict] = {}
    for e in ledger.entries():
        if not _in_window(e.ts_utc, since, until) or e.cost_usd <= 0:
            continue
        r = paid.setdefault(e.model, {"cost": 0.0, "calls": 0, "tokens": 0})
        r["cost"] += e.cost_usd
        r["calls"] += 1
        r["tokens"] += e.tokens_in + e.tokens_out
    if not paid:
        return []
    rates = sorted(
        ((m, _effective_rate(p)) for m, p in pricing.models.items() if _effective_rate(p) > 0),
        key=lambda x: x[1],
    )
    out: list[AdvisorRow] = []
    for model, r in paid.items():
        alt = next((x for x in rates if x[0] != model), None)
        if alt is None:
            continue
        alt_cost = (alt[1] * r["tokens"]) / 1_000_000
        savings = max(0.0, r["cost"] - alt_cost)
        out.append(AdvisorRow(model, r["cost"], r["calls"], r["tokens"], alt[0], alt[1],
                              alt_cost, savings, savings / r["cost"] if r["cost"] else 0.0))
    return sorted(out, key=lambda a: a.potential_savings_usd, reverse=True)


@dataclass
class BurnForecast:
    window_days: int
    avg_daily_cost_usd: float
    median_daily_cost_usd: float
    forecast_7d_usd: float
    forecast_30d_usd: float
    sample_days: int


def _slope(xs: list[float], ys: list[float]) -> float:
    n = len(xs)
    if n < 2:
        return 0.0
    mx = sum(xs) / n
    my = sum(ys) / n
    num = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    den = sum((x - mx) ** 2 for x in xs)
    return num / den if den else 0.0


def forecast_burn(ledger: Ledger, window_days: int = 30, until=None) -> BurnForecast:
    from .timeutil import day_key
    daily: dict[str, float] = {}
    for e in ledger.entries():
        if until is not None and datetime.fromisoformat(e.ts_utc.replace("Z", "+00:00")) > until:
            continue
        daily.setdefault(day_key(datetime.fromisoformat(e.ts_utc.replace("Z", "+00:00"))), 0.0)
        daily[day_key(datetime.fromisoformat(e.ts_utc.replace("Z", "+00:00")))] += e.cost_usd
    keys = sorted(daily)
    ys = [daily[k] for k in keys]
    xs = [float(i) for i in range(len(keys))]
    slope = _slope(xs, ys)
    mean = sum(ys) / len(ys) if ys else 0.0
    last = xs[-1] if xs else 0.0
    project = lambda n: max(0.0, mean + slope * (last + n))
    s = sorted(ys)
    median = 0.0 if not s else (s[len(s) // 2] if len(s) % 2 else (s[len(s) // 2 - 1] + s[len(s) // 2]) / 2)
    return BurnForecast(window_days, mean, median, project(7), project(30), len(ys))


@dataclass
class FinOpsReport:
    generated: str
    total_cost_usd: float
    total_tokens: int
    total_calls: int
    by_provider: list[SpendGroup] = field(default_factory=list)
    by_caller: list[SpendGroup] = field(default_factory=list)
    by_task: list[SpendGroup] = field(default_factory=list)
    by_model_realized: list[ModelRealized] = field(default_factory=list)
    advisor: list[AdvisorRow] = field(default_factory=list)
    forecast: Optional[BurnForecast] = None


def build_finops_report(ledger: Ledger, pricing: PricingCatalog, since=None, until=None,
                        window_days: int = 30, generated: str = "") -> FinOpsReport:
    cost = tokens = calls = 0
    cost = 0.0
    for e in ledger.entries():
        if not _in_window(e.ts_utc, since, until):
            continue
        cost += e.cost_usd
        tokens += e.tokens_in + e.tokens_out
        calls += 1
    return FinOpsReport(
        generated=generated,
        total_cost_usd=cost,
        total_tokens=tokens,
        total_calls=calls,
        by_provider=spend_by_dimension(ledger, "provider", since, until),
        by_caller=spend_by_dimension(ledger, "caller", since, until),
        by_task=spend_by_dimension(ledger, "task", since, until),
        by_model_realized=realized_rate_by_model(ledger, since, until),
        advisor=cheapest_equivalent_advisor(ledger, pricing, since, until),
        forecast=forecast_burn(ledger, window_days, until),
    )
