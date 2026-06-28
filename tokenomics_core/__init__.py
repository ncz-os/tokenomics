"""openclaw tokenomics - local-first LLM spend accounting (vendored Python port).

Host-neutral by design: the core has no coding-tool dependency. The ledger +
report shapes are wire-compatible (snake_case) with the Rust cost-reporting CLI
and the TypeScript ``@openclaw/tokenomics`` package.

This is a vendored copy so ``nv_tokenomics`` is fully self-contained - consumers
do not need to pull ``@openclaw/tokenomics`` from npm.
"""

from __future__ import annotations

from .host_adapter import HostAdapter, IngestOptions, UsageEvent, ingest, resolve_cost, to_ledger_entry
from .ledger import Ledger, LedgerEntry, Rollup, parse_period
from .pricing import ModelPrice, PricingCatalog
from .report import Bucket, Report, RowByModel, build_report, parse_gran
from .render import render_by_model, render_report, share_bar
from .timeutil import day_key, days_between, hour_key, month_key, parse_date, week_key, year_key

__all__ = [
    "HostAdapter", "IngestOptions", "UsageEvent", "ingest", "resolve_cost", "to_ledger_entry",
    "Ledger", "LedgerEntry", "Rollup", "parse_period",
    "ModelPrice", "PricingCatalog",
    "Bucket", "Report", "RowByModel", "build_report", "parse_gran",
    "render_by_model", "render_report", "share_bar",
    "day_key", "days_between", "hour_key", "month_key", "parse_date", "week_key", "year_key",
]
