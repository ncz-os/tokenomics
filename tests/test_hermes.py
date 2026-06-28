"""Hermes adapter — mapping a usage snapshot to a canonical ledger row.

No live Hermes needed: we exercise the snapshot field_map directly (the shape the
gateway status / `_get_usage` RPC emits) plus the on-disk snapshot reader."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from adapters.hermes import iter_sessions, usage_to_entry  # noqa: E402
from tokenomics_core.pricing import ModelPrice, PricingCatalog  # noqa: E402


def _catalog() -> PricingCatalog:
    cat = PricingCatalog()
    cat.models["hermes-llama-3.1-405b"] = ModelPrice(
        input_usd_per_mtok=3.0, output_usd_per_mtok=3.0
    )
    return cat


def test_usage_to_entry_prefers_cache_inclusive_counters():
    snap = {
        "session_id": "abc",
        "model": "hermes-llama-3.1-405b",
        "provider": "nousresearch",
        "session_start": "2026-06-28T00:00:00+00:00",
        "session_input_tokens": 1000,
        "session_output_tokens": 500,
        # prompt/completion are the fallback — must be ignored when input/output present
        "session_prompt_tokens": 1,
        "session_completion_tokens": 1,
    }
    e = usage_to_entry(snap, pricing=_catalog())
    assert e.tokens_in == 1000
    assert e.tokens_out == 500
    assert e.provider == "nousresearch"
    assert e.model == "hermes-llama-3.1-405b"
    assert e.ts_utc == "2026-06-28T00:00:00+00:00"
    # 1500 tokens @ $3/Mtok = $0.0045
    assert abs(e.cost_usd - 0.0045) < 1e-9


def test_usage_to_entry_falls_back_to_prompt_completion():
    snap = {"model": "x", "session_prompt_tokens": 200, "session_completion_tokens": 50}
    e = usage_to_entry(snap)
    assert e.tokens_in == 200
    assert e.tokens_out == 50
    assert e.provider == "hermes"  # default when snapshot omits provider
    assert e.cost_usd == 0.0  # no pricing -> never invent cost


def test_iter_sessions_reads_dir_and_skips_empty(tmp_path):
    (tmp_path / "a.json").write_text(json.dumps(
        {"session_id": "s1", "model": "hermes-llama-3.1-405b",
         "session_input_tokens": 10, "session_output_tokens": 4}))
    (tmp_path / "empty.json").write_text(json.dumps(
        {"session_id": "s2", "model": "x"}))  # no token signal -> skipped
    (tmp_path / "bad.json").write_text("{not json")  # ignored
    out = dict(iter_sessions(tmp_path, pricing=_catalog()))
    assert set(out) == {"s1"}
    assert out["s1"].tokens_in == 10
