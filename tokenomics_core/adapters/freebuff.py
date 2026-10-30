"""freebuff ingestion adapter — a coding-agent TUI's local prompt inbox.

Reads ``<inbox>/done/<name>.json`` record files. The record shape is still
evolving, so only ``id`` and ``completedAt`` are required; every other field is
optional and unknown keys are ignored. Fields used (all optional but the two)::

    id, completedAt, outcome, model, sessionMultiplier, newSession, attempt,
    paidFrom, balanceBefore.spendableHours, balanceAfter.spendableHours

Freebuff is metered in **hours x a multiplier, not tokens**, so rows carry
``provider="freebuff"``, ``tokens_in = tokens_out = 0``, ``calls = 1`` and a
*dollar-equivalent* ``cost_usd = hours_charged x rate``.

hours_charged
    1. ``balanceBefore.spendableHours`` and ``balanceAfter.spendableHours`` both
       present and ``before >= after``: the difference (the real charge, including
       any session minimum);
    2. else ``newSession`` is true: ``sessionMultiplier x 1 hour`` (multiplier
       defaults to 1);
    3. else 0 (no new charge).

rate (:class:`FreebuffRate`)
    Pool rate ``monthly_fee_usd / (pool_hours_per_day x days_per_month)`` —
    defaults ``8.0 / (50 x 30) = $0.005333/h`` — unless the record says the wallet
    paid (``paidFrom == "wallet"``), then ``wallet_usd_per_hour`` (default $0.05).

Dollar-equivalent method and its limits
    The pool rate is an *allocation of a flat fee*, not a price anyone was billed
    per hour: the subscription costs the same whether or not the daily pool hours
    are used, so every unused hour raises the real cost per hour consumed (e.g.
    using 5 of 50 pool-hours/day makes the realized rate 10x the figure here). The
    numbers are for comparison and forecasting against metered tools only, and
    should not be read as invoiced spend. Wallet-paid rows are real per-hour
    charges at the wallet rate.

Rows with ``outcome == "session_ended"`` and no charge are skipped. Other
zero-charge outcomes are kept as ``cost_usd = 0`` calls.

Idempotence: :func:`ingest` remembers every ingested ``id``+``attempt`` in a
sidecar ``<ledger>.freebuff.state.json`` (atomic write; crash-safe ordering in
:mod:`tokenomics_core.adapters._ingest`). Unreadable/half-written files are
skipped and retried on the next run. Single writer per ledger is assumed.
"""

from __future__ import annotations

import json
import math
import os
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from tokenomics_core.ledger import Ledger, LedgerEntry

from ._ingest import (
    IngestResult,
    as_ledger,
    coerce_since,
    commit,
    load_state,
    parse_ts,
    recover,
    state_path,
)

HOST = "freebuff"
#: Env var naming the inbox directory (no built-in default path).
ENV_SOURCE = "TOKENOMICS_FREEBUFF_INBOX"


@dataclass(frozen=True)
class FreebuffRate:
    """Dollar-equivalent rate configuration (see module docstring)."""

    monthly_fee_usd: float = 8.0
    pool_hours_per_day: float = 50
    wallet_usd_per_hour: float = 0.05
    days_per_month: float = 30

    def __post_init__(self) -> None:
        if self.monthly_fee_usd < 0 or self.wallet_usd_per_hour < 0:
            raise ValueError("rates must be non-negative")
        if self.pool_hours_per_day <= 0 or self.days_per_month <= 0:
            raise ValueError("pool_hours_per_day and days_per_month must be positive")

    @property
    def pool_usd_per_hour(self) -> float:
        """Realized pool rate: ``monthly_fee_usd / (pool_hours_per_day x days_per_month)``."""
        return self.monthly_fee_usd / (self.pool_hours_per_day * self.days_per_month)

    @classmethod
    def from_env(cls, env=None) -> FreebuffRate:
        """Defaults overridden by ``TOKENOMICS_FREEBUFF_{MONTHLY_FEE_USD,POOL_HOURS_PER_DAY,
        WALLET_USD_PER_HOUR,DAYS_PER_MONTH}``."""
        env = os.environ if env is None else env
        kw = {}
        for name in (
            "monthly_fee_usd",
            "pool_hours_per_day",
            "wallet_usd_per_hour",
            "days_per_month",
        ):
            v = env.get(f"TOKENOMICS_FREEBUFF_{name.upper()}")
            if v not in (None, ""):
                try:
                    kw[name] = float(v)
                except ValueError as e:
                    raise ValueError(
                        f"TOKENOMICS_FREEBUFF_{name.upper()}: not a number: {v!r}"
                    ) from e
        return cls(**kw)


def _num(v) -> float | None:
    return (
        float(v)
        if isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)
        else None
    )


def _spendable(rec: dict, key: str) -> float | None:
    b = rec.get(key)
    return _num(b.get("spendableHours")) if isinstance(b, dict) else None


def hours_charged(rec: dict) -> float:
    """Hours charged for one record (module docstring, ``hours_charged``)."""
    before, after = _spendable(rec, "balanceBefore"), _spendable(rec, "balanceAfter")
    if before is not None and after is not None and before >= after:
        return before - after
    if rec.get("newSession") is True:
        mult = _num(rec.get("sessionMultiplier"))
        return mult if mult is not None and mult >= 0 else 1.0
    return 0.0


def _model_key(rec: dict) -> str:
    m = rec.get("model")
    if isinstance(m, dict):
        m = m.get("key")
    return m if isinstance(m, str) and m else "unknown"


def record_key(rec: dict) -> str:
    """Dedup key: ``<id>#<attempt>`` (attempt defaults to 0)."""
    att = rec.get("attempt")
    return f"{rec['id']}#{att if isinstance(att, int) and not isinstance(att, bool) else 0}"


def parse_record(
    rec, rate: FreebuffRate | None = None
) -> tuple[tuple[str, LedgerEntry] | None, str | None]:
    """Map one decoded record to ``((key, entry), None)`` or ``(None, skip_reason)``.

    Reasons: ``malformed`` (not an object, or ``id`` missing), ``bad_ts``
    (``completedAt`` missing/unparseable), ``session_ended`` (no charge).
    """
    rate = rate or FreebuffRate()
    if (
        not isinstance(rec, dict)
        or not isinstance(rec.get("id"), (str, int))
        or isinstance(rec.get("id"), bool)
    ):
        return None, "malformed"
    ts = parse_ts(rec.get("completedAt"))
    if ts is None:
        return None, "bad_ts"
    hours = hours_charged(rec)
    if rec.get("outcome") == "session_ended" and hours == 0:
        return None, "session_ended"
    usd_per_hour = (
        rate.wallet_usd_per_hour
        if rec.get("paidFrom") == "wallet"
        else rate.pool_usd_per_hour
    )
    return (
        record_key(rec),
        LedgerEntry(
            ts_utc=ts.isoformat(),
            provider="freebuff",
            model=_model_key(rec),
            tokens_in=0,
            tokens_out=0,
            cost_usd=hours * usd_per_hour,
        ),
    ), None


def resolve_source(path: str | os.PathLike | None = None) -> Path:
    """``path`` or ``$TOKENOMICS_FREEBUFF_INBOX``; there is no built-in default."""
    path = path or os.environ.get(ENV_SOURCE)
    if not path:
        raise ValueError(f"freebuff inbox not set: pass a path or set {ENV_SOURCE}")
    if not Path(path).is_dir():
        raise FileNotFoundError(f"freebuff inbox not found: {path}")
    return Path(path)


def scan(inbox: str | os.PathLike, rate: FreebuffRate | None = None):
    """Parse every ``done/*.json``. Returns ``(items, skipped)`` where ``items`` is
    ``[(key, entry)]`` sorted by ``(ts_utc, key)`` and ``skipped`` is ``{reason: count}``.
    Unreadable or half-written files count as ``malformed``."""
    items: list[tuple[str, LedgerEntry]] = []
    skipped: dict[str, int] = {}
    done = Path(inbox) / "done"
    for f in sorted(done.glob("*.json")) if done.is_dir() else []:
        try:
            parsed, reason = parse_record(
                json.loads(f.read_text(encoding="utf-8")), rate
            )
        except (OSError, ValueError):
            parsed, reason = None, "malformed"
        if parsed is None:
            skipped[reason] = skipped.get(reason, 0) + 1
        else:
            items.append(parsed)
    items.sort(key=lambda kv: (kv[1].ts_utc, kv[0]))
    return items, skipped


def iter_records(
    inbox: str | os.PathLike, rate: FreebuffRate | None = None
) -> Iterator[tuple[str, LedgerEntry]]:
    """Yield ``(record_key, LedgerEntry)`` for each chargeable record (cf. goose ``iter_sessions``)."""
    yield from scan(inbox, rate)[0]


def ingest(
    inbox: str | os.PathLike,
    ledger: Ledger | str | os.PathLike,
    *,
    since=None,
    rate: FreebuffRate | None = None,
) -> IngestResult:
    """Append not-yet-ingested records to ``ledger``; re-running appends nothing new.

    ``since`` (datetime or ISO string) drops records completed before that instant;
    they are still marked seen. ``rate`` defaults to :meth:`FreebuffRate.from_env`.
    """
    inbox = resolve_source(inbox)
    rate = rate or FreebuffRate.from_env()
    led = as_ledger(ledger)
    spath = state_path(led, HOST)
    state = load_state(spath)
    result = IngestResult(recovered=recover(led, spath, state))
    if result.recovered:
        state = load_state(spath)
    seen = set(state.get("seen", []))
    items, skipped = scan(inbox, rate)
    result.skipped.update(skipped)
    cutoff = coerce_since(since)
    new = [(k, e) for k, e in items if k not in seen]
    entries = []
    for _k, e in new:
        if cutoff is not None and parse_ts(e.ts_utc) < cutoff:
            result.skip("since")
        else:
            entries.append(e)
    commit(led, spath, state, entries, {"seen": sorted(seen | {k for k, _ in new})})
    result.appended = len(entries)
    return result
