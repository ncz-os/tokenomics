"""Terminal rendering for tokenomics reports. Dependency-free ANSI so it drops
into any host's CLI. Honors NO_COLOR and non-TTY (plain output)."""

from __future__ import annotations

import os
import sys

from .report import Report, RowByModel

_USE_COLOR = not os.environ.get("NO_COLOR") and sys.stdout.isatty()


def _paint(s: str, code: str) -> str:
    return f"{code}{s}\x1b[0m" if _USE_COLOR else s


_dim = lambda s: _paint(s, "\x1b[2m")
_bold = lambda s: _paint(s, "\x1b[1m")
_green = lambda s: _paint(s, "\x1b[32m")
_cyan = lambda s: _paint(s, "\x1b[36m")
_yellow = lambda s: _paint(s, "\x1b[33m")


def _usd(n: float) -> str:
    return f"${n:,.2f}"


def _tok(n: float) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.2f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}k"
    return str(int(n))


def share_bar(free_tokens: int, total_tokens: int, width: int = 24) -> str:
    """A unicode bar showing the free share of total tokens."""
    if total_tokens <= 0:
        return f"{'·' * width} 0% free"
    frac = max(0.0, min(1.0, free_tokens / total_tokens))
    filled = round(frac * width)
    bar = _green("█" * filled) + _dim("░" * (width - filled))
    return f"{bar} {frac * 100:.0f}% free"


def render_by_model(rows: list[RowByModel], limit: int = 12) -> str:
    """A compact by-model table (top ``limit`` rows by cost)."""
    top = rows[:limit]
    name_w = max([5] + [len(r.model) for r in top])
    head = (
        _dim("model".ljust(name_w)) + "  " + _dim("calls".rjust(7)) + "  "
        + _dim("tokens".rjust(9)) + "  " + _dim("cost".rjust(10)) + "  " + _dim("tag")
    )
    lines = []
    for r in top:
        tag = _yellow("paid") if r.billed else _green("free")
        lines.append(
            r.model.ljust(name_w) + "  " + str(r.calls).rjust(7) + "  "
            + _tok(r.tokens).rjust(9) + "  " + _usd(r.cost_usd).rjust(10) + "  " + tag
        )
    return "\n".join([head, *lines])


def render_report(rep: Report) -> str:
    """Full report render: headline, free-share bar, then by-model table."""
    arrow = "\u2192"
    if rep.period:
        window = f"{rep.period} ({rep.since} {arrow} {rep.until}, {rep.days}d)"
    else:
        window = f"{rep.since} {arrow} {rep.until} ({rep.days}d)"
    baseline = rep.baseline_model or "baseline"
    spent_meta = _dim(f"({rep.total_calls} calls, {_tok(rep.total_tokens)} tok)")
    cf_meta = _dim(f"all tokens @ {baseline} ({_usd(rep.baseline_usd_per_mtok)}/Mtok)")
    headline = "\n".join([
        _bold("Tokenomics"),
        _dim(window),
        "",
        f"{_dim('spent')}        {_bold(_usd(rep.total_cost_usd))}  {spent_meta}",
        f"{_dim('avoided')}      {_green(_usd(rep.avoided_usd))}  {_dim('free tokens valued at baseline')}",
        f"{_dim('counterfactual')} {_cyan(_usd(rep.counterfactual_usd))}  {cf_meta}",
        "",
        f"{_dim('free share')}   {share_bar(rep.free_tokens, rep.total_tokens)}",
    ])
    return f"{headline}\n\n{render_by_model(rep.by_model)}"
