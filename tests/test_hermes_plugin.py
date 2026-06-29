"""Hermes plugin — registration contract + end-to-end command against a real
temp state.db (built with the real `sessions` column shape). No live Hermes."""
import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import hermes_plugin  # noqa: E402

_SCHEMA = """
CREATE TABLE sessions (
    id TEXT PRIMARY KEY, source TEXT, started_at REAL, ended_at REAL,
    model TEXT, model_config TEXT, billing_provider TEXT,
    input_tokens INTEGER DEFAULT 0, output_tokens INTEGER DEFAULT 0,
    estimated_cost_usd REAL, actual_cost_usd REAL, cost_source TEXT
);
"""


def _state_db(tmp_path):
    p = tmp_path / "state.db"
    con = sqlite3.connect(p)
    con.executescript(_SCHEMA)
    con.execute(
        "INSERT INTO sessions (id, source, started_at, ended_at, model, model_config, "
        "billing_provider, input_tokens, output_tokens, estimated_cost_usd, actual_cost_usd, cost_source) "
        "VALUES ('s1','cli',1.0,2.0,'hermes-4-405b',NULL,'nousresearch',120000,30000,NULL,0.45,'provider')"
    )
    con.commit()
    con.close()
    return p


class _FakeCtx:
    """Captures register_cli_command calls (the Hermes plugin contract)."""
    def __init__(self):
        self.commands = {}

    def register_cli_command(self, *, name, help, setup_fn, handler_fn=None, description=""):
        self.commands[name] = dict(help=help, setup_fn=setup_fn,
                                   handler_fn=handler_fn, description=description)


def test_register_adds_tokenomics_command():
    ctx = _FakeCtx()
    hermes_plugin.register(ctx)
    assert "tokenomics" in ctx.commands
    cmd = ctx.commands["tokenomics"]
    assert callable(cmd["setup_fn"]) and callable(cmd["handler_fn"])
    # setup_fn must populate an argparse subparser without error
    import argparse
    p = argparse.ArgumentParser()
    cmd["setup_fn"](p)
    ns = p.parse_args([])
    assert hasattr(ns, "db") and hasattr(ns, "ledger") and hasattr(ns, "finops")


def test_handle_ingests_state_db_into_ledger(tmp_path, capsys):
    db = _state_db(tmp_path)
    ledger = tmp_path / "ledger.jsonl"
    # ingest-only: sync state.db -> ledger, no report
    rc = hermes_plugin._handle(SimpleNamespace(
        db=str(db), ledger=str(ledger), pricing=None, days=30,
        finops=False, ingest_only=True))
    assert rc == 0
    rows = [r for r in ledger.read_text().splitlines() if r.strip()]
    assert len(rows) == 1
    assert '"cost_usd": 0.45' in rows[0] and '"provider": "nousresearch"' in rows[0]

    # full report path returns 0 and prints a spend line
    rc = hermes_plugin._handle(SimpleNamespace(
        db=str(db), ledger=str(ledger), pricing=None, days=3650,
        finops=False, ingest_only=False))
    assert rc == 0
    out = capsys.readouterr().out
    assert "spent" in out.lower()
