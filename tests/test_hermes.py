"""Hermes adapter — read the ~/.hermes/state.db `sessions` store (host cost) and
the secondary live-snapshot mapping. No live Hermes needed: we build a minimal
state.db with the real column shape and exercise the field_map + cost precedence."""
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from adapters.hermes import iter_sessions, usage_to_entry  # noqa: E402
from tokenomics_core.pricing import ModelPrice, PricingCatalog  # noqa: E402

_SCHEMA = """
CREATE TABLE sessions (
    id TEXT PRIMARY KEY, source TEXT, started_at REAL, ended_at REAL,
    model TEXT, model_config TEXT, billing_provider TEXT,
    input_tokens INTEGER DEFAULT 0, output_tokens INTEGER DEFAULT 0,
    estimated_cost_usd REAL, actual_cost_usd REAL, cost_source TEXT
);
"""


def _db(tmp_path, rows):
    p = tmp_path / "state.db"
    con = sqlite3.connect(p)
    con.executescript(_SCHEMA)
    con.executemany(
        "INSERT INTO sessions (id, source, started_at, ended_at, model, model_config, "
        "billing_provider, input_tokens, output_tokens, estimated_cost_usd, actual_cost_usd, cost_source) "
        "VALUES (:id,:source,:started_at,:ended_at,:model,:model_config,:billing_provider,"
        ":input_tokens,:output_tokens,:estimated_cost_usd,:actual_cost_usd,:cost_source)",
        rows,
    )
    con.commit()
    con.close()
    return p


def _catalog():
    cat = PricingCatalog()
    cat.models["hermes-405b"] = ModelPrice(input_usd_per_mtok=3.0, output_usd_per_mtok=3.0)
    return cat


def test_iter_sessions_cost_precedence_and_skip(tmp_path):
    rows = [
        # actual cost wins over estimate and catalog
        dict(id="s1", source="cli", started_at=1.0, ended_at=2.0, model="hermes-405b",
             model_config=None, billing_provider="nousresearch", input_tokens=1000,
             output_tokens=500, estimated_cost_usd=0.01, actual_cost_usd=0.05, cost_source="provider"),
        # no actual -> estimate
        dict(id="s2", source="cli", started_at=1.0, ended_at=None, model="hermes-405b",
             model_config=None, billing_provider="nousresearch", input_tokens=200,
             output_tokens=0, estimated_cost_usd=0.02, actual_cost_usd=None, cost_source="estimate"),
        # no host cost -> catalog (1500 tok @ $3/Mtok = 0.0045)
        dict(id="s3", source="cli", started_at=1.0, ended_at=None, model="hermes-405b",
             model_config=None, billing_provider=None, input_tokens=1000,
             output_tokens=500, estimated_cost_usd=None, actual_cost_usd=None, cost_source=None),
        # no spend signal -> skipped
        dict(id="s4", source="cli", started_at=1.0, ended_at=None, model="hermes-405b",
             model_config=None, billing_provider=None, input_tokens=0,
             output_tokens=0, estimated_cost_usd=None, actual_cost_usd=None, cost_source=None),
    ]
    out = dict(iter_sessions(_db(tmp_path, rows), pricing=_catalog()))
    assert set(out) == {"s1", "s2", "s3"}
    assert out["s1"].cost_usd == 0.05 and out["s1"].provider == "nousresearch"
    assert out["s2"].cost_usd == 0.02
    assert abs(out["s3"].cost_usd - 0.0045) < 1e-9
    assert out["s3"].provider == "hermes"  # default when billing_provider null


def test_provider_falls_back_to_model_config(tmp_path):
    rows = [dict(id="s1", source="cli", started_at=1.0, ended_at=None, model=None,
                 model_config='{"provider":"openrouter","model":"x-large"}',
                 billing_provider="custom", input_tokens=10, output_tokens=4,
                 estimated_cost_usd=None, actual_cost_usd=None, cost_source=None)]
    e = dict(iter_sessions(_db(tmp_path, rows)))["s1"]
    assert e.provider == "openrouter"  # bare "custom" bucket -> routable model_config.provider
    assert e.model == "x-large"


def test_live_snapshot_mapping():
    snap = {"model": "hermes-405b", "provider": "nousresearch",
            "session_input_tokens": 1000, "session_output_tokens": 500,
            "session_prompt_tokens": 1, "session_completion_tokens": 1}
    e = usage_to_entry(snap, pricing=_catalog())
    assert e.tokens_in == 1000 and e.tokens_out == 500  # cache-inclusive preferred
    assert abs(e.cost_usd - 0.0045) < 1e-9
