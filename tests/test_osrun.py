"""osrun adapter — a budget-capped runner's JSONL ledger (pass + summary records).
Inline fixtures only; the repo's own ``Ledger`` reader proves the rows we append are valid."""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tokenomics_core import commands
from tokenomics_core.adapters import _ingest, osrun
from tokenomics_core.ledger import Ledger

PASS1 = {
    "task": "t1",
    "pass": "pass1",
    "model": "claude-opus-5-5",
    "usd": 0.046593,
    "tokens": {
        "total": 45588,
        "input": 2,
        "output": 1241,
        "reasoning": 48,
        "cache": {"write": 671, "read": 43626},
    },
    "ts": "2026-10-29T12:00:00Z",
}
PASS2 = {
    "task": "t1",
    "pass": "pass2",
    "model": "some-open-model",
    "usd": 0.0,
    "tokens": {"total": 10, "input": 4, "output": 6},
    "ts": "2026-10-29T13:30:00+01:00",
}
SUMMARY = {"task": "t1", "summary": {"usd": 0.046593, "passes": 2}}


def _src(tmp_path, *lines, name="runner.jsonl", tail="\n"):
    p = tmp_path / name
    p.write_text(
        "".join(
            json.dumps(x) + "\n" if isinstance(x, dict) else x for x in lines
        ).rstrip("\n")
        + tail
    )
    return p


def _append(p, *recs):
    with p.open("a") as fh:
        for r in recs:
            fh.write(json.dumps(r) + "\n")


def _ledger_bytes(led):
    return led.path.read_bytes() if led.path.exists() else b""


def test_pass_record_mapping(tmp_path):
    src = _src(tmp_path, PASS1)
    led = Ledger(tmp_path / "ledger.jsonl")
    res = osrun.ingest(src, led)
    assert res.appended == 1 and res.skipped_total == 0
    (e,) = led.entries()
    assert e.provider == "anthropic" and e.model == "claude-opus-5-5"
    assert e.tokens_in == 2 + 671 + 43626  # input + cache.write + cache.read
    assert e.tokens_out == 1241 + 48  # output + reasoning
    assert e.tokens_in + e.tokens_out == PASS1["tokens"]["total"]
    assert e.cost_usd == 0.046593  # host-authoritative, no catalog
    assert (
        e.ts_utc == "2026-10-29T12:00:00+00:00" and e.calls == 1 and e.violation is None
    )
    assert "tag" not in json.loads(led.path.read_text())


def test_unknown_provider_missing_cache_and_ts_normalized(tmp_path):
    src = _src(
        tmp_path,
        PASS2,
        {
            **PASS1,
            "model": "claude-x",
            "tokens": {"input": 5},
            "ts": "2026-10-29T12:00:00",
        },
    )
    led = Ledger(tmp_path / "ledger.jsonl")
    osrun.ingest(src, led)
    a, b = led.entries()
    assert a.provider == "unknown" and (a.tokens_in, a.tokens_out) == (4, 6)
    assert a.ts_utc == "2026-10-29T12:30:00+00:00"  # +01:00 -> UTC
    assert b.provider == "anthropic" and (b.tokens_in, b.tokens_out) == (
        5,
        0,
    )  # no cache block, naive ts = UTC


def test_missing_model_and_tokens_block(tmp_path):
    src = _src(
        tmp_path, {"usd": 0.5, "ts": "2026-10-29T12:00:00Z", "future_key": {"x": 1}}
    )
    led = Ledger(tmp_path / "ledger.jsonl")
    osrun.ingest(src, led)
    (e,) = led.entries()
    assert (e.model, e.provider, e.tokens_in, e.tokens_out, e.cost_usd) == (
        "unknown",
        "unknown",
        0,
        0,
        0.5,
    )


def test_summaries_and_lines_without_usd_are_skipped_not_double_counted(tmp_path):
    src = _src(tmp_path, PASS1, SUMMARY, {"task": "t1", "note": "no usd"}, PASS2)
    led = Ledger(tmp_path / "ledger.jsonl")
    res = osrun.ingest(src, led)
    assert res.appended == 2
    assert res.skipped == {"summary": 1, "no_usd": 1}
    assert sum(e.cost_usd for e in led.entries()) == pytest.approx(
        0.046593
    )  # summary usd not added


def test_malformed_lines_are_skipped_and_counted(tmp_path):
    src = _src(
        tmp_path,
        PASS1,
        "not json\n",
        "[1, 2]\n",
        "\n",
        {**PASS1, "usd": "x"},
        {**PASS1, "usd": -1},
        {**PASS1, "ts": "garbage"},
        {k: v for k, v in PASS1.items() if k != "ts"},
        PASS2,
    )
    led = Ledger(tmp_path / "ledger.jsonl")
    res = osrun.ingest(src, led)
    assert res.appended == 2
    assert res.skipped == {"malformed": 4, "bad_ts": 2}


def test_truncated_last_line_waits_until_complete(tmp_path):
    full = json.dumps(PASS2)
    src = _src(
        tmp_path, PASS1, full[:25], tail=""
    )  # runner killed/still writing mid-line
    led = Ledger(tmp_path / "ledger.jsonl")
    res = osrun.ingest(src, led)
    assert res.appended == 1 and res.skipped == {"truncated": 1}
    src.write_text(json.dumps(PASS1) + "\n" + full + "\n")  # runner finishes the line
    res = osrun.ingest(src, led)
    assert res.appended == 1 and res.skipped == {}
    assert [e.model for e in led.entries()] == ["claude-opus-5-5", "some-open-model"]


def test_complete_last_line_without_newline_is_consumed_once(tmp_path):
    src = _src(tmp_path, PASS1, tail="")
    led = Ledger(tmp_path / "ledger.jsonl")
    assert osrun.ingest(src, led).appended == 1
    with src.open("a") as fh:
        fh.write("\n")  # the runner's newline arrives later
    assert osrun.ingest(src, led).appended == 0
    assert len(led.entries()) == 1


def test_double_run_is_idempotent_and_incremental(tmp_path):
    src = _src(tmp_path, PASS1, SUMMARY)
    led = Ledger(tmp_path / "ledger.jsonl")
    assert osrun.ingest(src, led).appended == 1
    before = _ledger_bytes(led)
    again = osrun.ingest(src, led)
    assert again.appended == 0 and again.skipped_total == 0
    assert _ledger_bytes(led) == before
    _append(src, PASS2, SUMMARY)
    assert osrun.ingest(src, led).appended == 1  # only the new pass
    assert osrun.ingest(src, led).appended == 0
    assert len(led.entries()) == 2


def test_state_sidecar_is_next_to_ledger_and_has_no_temp_leftover(tmp_path):
    led = Ledger(tmp_path / "out" / "ledger.jsonl")
    osrun.ingest(_src(tmp_path, PASS1), led)
    st = json.loads((tmp_path / "out" / "ledger.jsonl.osrun.state.json").read_text())
    assert (
        st["offset"] == (tmp_path / "runner.jsonl").stat().st_size
        and "pending" not in st
    )
    assert [
        p.name for p in (tmp_path / "out").iterdir() if p.name.endswith(".tmp")
    ] == []


def test_corrupt_state_raises_instead_of_reingesting(tmp_path):
    led = Ledger(tmp_path / "ledger.jsonl")
    src = _src(tmp_path, PASS1)
    osrun.ingest(src, led)
    (tmp_path / "ledger.jsonl.osrun.state.json").write_text("{trunc")
    with pytest.raises(ValueError, match="corrupt ingest state"):
        osrun.ingest(src, led)
    assert len(led.entries()) == 1


def test_rewritten_source_resumes_after_last_known_line(tmp_path):
    src = _src(tmp_path, PASS1, PASS2)
    led = Ledger(tmp_path / "ledger.jsonl")
    osrun.ingest(src, led)
    # runner compacts its ledger: an old line is dropped, offsets shift, one pass is new
    pass3 = {**PASS2, "pass": "pass3", "ts": "2026-10-30T00:00:00Z"}
    src.write_text(json.dumps(PASS2) + "\n" + json.dumps(pass3) + "\n")
    assert osrun.ingest(src, led).appended == 1
    assert [e.ts_utc for e in led.entries()][-1] == "2026-10-30T00:00:00+00:00"
    assert osrun.ingest(src, led).appended == 0


def test_since_filters_but_advances_high_water(tmp_path):
    src = _src(tmp_path, PASS1, PASS2)  # 12:00Z and 12:30Z
    led = Ledger(tmp_path / "ledger.jsonl")
    res = osrun.ingest(src, led, since="2026-10-29T12:15:00Z")
    assert res.appended == 1 and res.skipped == {"since": 1}
    assert [e.model for e in led.entries()] == ["some-open-model"]
    assert osrun.ingest(src, led).appended == 0  # the older pass is not reconsidered


# --- crash safety: append and state write cannot be atomic together, so the
# order is intent -> append -> commit, and recovery makes each cut point exact.


class _Crash(Exception):
    pass


def _crash_on_state_write(monkeypatch, nth):
    real, calls = _ingest.write_state, []

    def flaky(path, state):
        calls.append(1)
        if len(calls) == nth:
            raise _Crash()
        real(path, state)

    monkeypatch.setattr(_ingest, "write_state", flaky)


@pytest.mark.parametrize(
    "nth, rows_in_ledger_after_crash, recovered, appended", [(1, 0, 0, 2), (2, 2, 2, 0)]
)
def test_crash_around_append_neither_loses_nor_duplicates(
    tmp_path, monkeypatch, nth, rows_in_ledger_after_crash, recovered, appended
):
    src = _src(tmp_path, PASS1, PASS2)
    led = Ledger(tmp_path / "ledger.jsonl")
    with monkeypatch.context() as m:
        _crash_on_state_write(
            m, nth
        )  # 1 = before the append, 2 = after append, before commit
        with pytest.raises(_Crash):
            osrun.ingest(src, led)
    assert len(led.entries()) == rows_in_ledger_after_crash
    res = osrun.ingest(src, led)
    assert len(led.entries()) == 2  # exactly once, either way
    assert (res.recovered, res.appended) == (recovered, appended)
    assert osrun.ingest(src, led).appended == 0
    assert len(led.entries()) == 2
    assert "pending" not in json.loads(
        (tmp_path / "ledger.jsonl.osrun.state.json").read_text()
    )


def test_crash_after_intent_before_any_byte_is_appended(tmp_path, monkeypatch):
    src = _src(tmp_path, PASS1, PASS2)
    led = Ledger(tmp_path / "ledger.jsonl")

    def dead(path, data):
        raise _Crash()

    with monkeypatch.context() as m:
        m.setattr(_ingest, "_append_bytes", dead)
        with pytest.raises(_Crash):
            osrun.ingest(src, led)
    assert led.entries() == []
    res = osrun.ingest(
        src, led
    )  # the intent record makes the retry append exactly the batch
    assert (res.recovered, res.appended) == (2, 0) and len(led.entries()) == 2
    assert osrun.ingest(src, led).appended == 0 and len(led.entries()) == 2


def test_crash_during_append_completes_the_torn_block(tmp_path, monkeypatch):
    src = _src(tmp_path, PASS1, PASS2)
    led = Ledger(tmp_path / "ledger.jsonl")
    real = _ingest._append_bytes

    def torn(path, data):
        real(path, data[: len(data) // 2])
        raise _Crash()

    with monkeypatch.context() as m:
        m.setattr(_ingest, "_append_bytes", torn)
        with pytest.raises(_Crash):
            osrun.ingest(src, led)
    assert len(led.entries()) < 2  # a half-written line is not a valid row
    osrun.ingest(src, led)
    assert [e.model for e in led.entries()] == ["claude-opus-5-5", "some-open-model"]
    seen = []
    assert (
        len(led.entries(on_malformed=lambda n, raw: seen.append(n))) == 2 and seen == []
    )


def test_crash_recovery_reports_rows_it_completed(tmp_path, monkeypatch):
    src = _src(tmp_path, PASS1)
    led = Ledger(tmp_path / "ledger.jsonl")
    with monkeypatch.context() as m:
        _crash_on_state_write(m, 2)
        with pytest.raises(_Crash):
            osrun.ingest(src, led)
    res = osrun.ingest(src, led)
    assert (res.recovered, res.appended) == (1, 0)


def test_ledger_lines_round_trip_with_the_repos_reader(tmp_path):
    led = Ledger(tmp_path / "ledger.jsonl")
    led.path.write_text(
        '{"ts_utc": "2026-01-01T00:00:00+00:00", "provider": "p", "model": "m", '
        '"tokens_in": 1, "tokens_out": 1, "cost_usd": 0.1}'
    )  # torn tail, no newline
    osrun.ingest(_src(tmp_path, PASS1, PASS2), led)
    bad = []
    rows = led.entries(on_malformed=lambda n, raw: bad.append(n))
    assert bad == [] and len(rows) == 3  # our rows landed on their own lines
    for line in led.path.read_text().splitlines():
        assert set(json.loads(line)) <= {
            "ts_utc",
            "provider",
            "model",
            "tokens_in",
            "tokens_out",
            "cost_usd",
        }
    assert led.by_model()["claude-opus-5-5"].tokens_in == 44299


def test_missing_source_and_unset_env(tmp_path, monkeypatch):
    monkeypatch.delenv(osrun.ENV_SOURCE, raising=False)
    with pytest.raises(ValueError, match=osrun.ENV_SOURCE):
        osrun.resolve_source(None)
    with pytest.raises(FileNotFoundError):
        osrun.ingest(tmp_path / "nope.jsonl", Ledger(tmp_path / "l.jsonl"))


def test_commands_ingest_uses_env_and_since(tmp_path, monkeypatch):
    src = _src(tmp_path, PASS1, PASS2)
    monkeypatch.setenv(osrun.ENV_SOURCE, str(src))
    ledger = str(tmp_path / "ledger.jsonl")
    assert commands.ingest("osrun", ledger) == 2
    assert commands.ingest("osrun", ledger) == 0
    with pytest.raises(ValueError):
        commands.ingest("osrun", ledger, source=str(tmp_path / "missing.jsonl"))
