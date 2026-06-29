"""Hermes plugin — ``hermes tokenomics``.

A standalone Hermes (NousResearch/hermes-agent) plugin that adds an operator
command surfacing LLM spend from Hermes's *own* session store
(``~/.hermes/state.db``) through the shared host-neutral tokenomics core. It is
read-only and requires **no** change to Hermes core — Hermes already persists
per-session tokens and cost (``input_tokens``/``output_tokens``/``actual_cost_usd``/
``estimated_cost_usd``) to that SQLite store.

This mirrors ``@openclaw/tokenomics``: a self-contained host package (shared
ledger/pricing/report/finops core + the Hermes ingestion adapter). It does **not**
depend on any NVIDIA-internal module.

Discovery (either path):
  * pip: ``pip install tokenomics`` exposes the ``hermes_agent.plugins`` entry
    point below; Hermes auto-discovers it.
  * drop-in: copy this package into ``~/.hermes/plugins/tokenomics/``.

Hermes loads the module and calls ``register(ctx)`` (the Hermes plugin contract).
The command:
  hermes tokenomics            # sync state.db -> ledger, print spend report
  hermes tokenomics --finops   # the FinOps view (allocation/realized-rate/advisor/forecast)
  hermes tokenomics --ingest-only
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

DEFAULT_DB = Path.home() / ".hermes" / "state.db"
DEFAULT_LEDGER = Path.home() / ".hermes" / "tokenomics-ledger.jsonl"


def _setup(subparser) -> None:
    """Populate the ``hermes tokenomics`` argparse subparser (Hermes plugin contract)."""
    subparser.add_argument("--db", default=str(DEFAULT_DB),
                           help="Hermes session store (default ~/.hermes/state.db)")
    subparser.add_argument("--ledger", default=str(DEFAULT_LEDGER),
                           help="canonical ledger path (default ~/.hermes/tokenomics-ledger.jsonl)")
    subparser.add_argument("--pricing", default=None,
                           help="pricing.json — estimate cost only when Hermes recorded none")
    subparser.add_argument("--days", type=int, default=30,
                           help="report/finops window in days (default 30)")
    subparser.add_argument("--finops", action="store_true",
                           help="emit the FinOps view (allocation/realized-rate/advisor/forecast) as JSON")
    subparser.add_argument("--ingest-only", action="store_true",
                           help="only sync state.db into the ledger; print nothing")


def _handle(args) -> int:
    """Dispatch ``hermes tokenomics`` — sync the store, then report/finops."""
    import cli  # shared CLI command implementations (this package)

    # 1. Pull Hermes's state.db into the canonical ledger (dedup per session).
    cli.cmd_ingest(SimpleNamespace(
        host="hermes", ledger=args.ledger, db=args.db, pricing=args.pricing))
    if getattr(args, "ingest_only", False):
        return 0

    # 2. Report (or the FinOps view) over the ledger.
    view = SimpleNamespace(ledger=args.ledger, pricing=args.pricing, days=args.days)
    return cli.cmd_finops(view) if getattr(args, "finops", False) else cli.cmd_report(view)


def register(ctx) -> None:
    """Hermes plugin entry point — register the ``hermes tokenomics`` command."""
    ctx.register_cli_command(
        name="tokenomics",
        help="LLM-spend report + FinOps from Hermes's session store",
        setup_fn=_setup,
        handler_fn=_handle,
        description=(
            "Reads Hermes's own ~/.hermes/state.db (per-session tokens + cost) into "
            "a canonical tokenomics ledger and prints a spend report or the FinOps "
            "view. Read-only; no Hermes core changes; cost is host-authoritative when "
            "Hermes recorded it, else estimated from --pricing."
        ),
    )
