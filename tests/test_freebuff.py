"""freebuff adapter — a coding-agent TUI's local prompt inbox (``done/<name>.json``),
metered in hours x multiplier. Inline fixtures; rows are checked with the repo's own ``Ledger``."""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tokenomics_core import commands
from tokenomics_core.adapters import _ingest, freebuff
from tokenomics_core.adapters.freebuff import FreebuffRate
from tokenomics_core.ledger import Ledger

POOL = 8.0 / (50 * 30)  # 0.005333.. USD per pool hour


def _rec(i, **kw):
    base = {
        "id": i,
        "completedAt": "2026-10-29T12:00:00Z",
        "outcome": "completed",
        "model": "fb-model",
    }
    base.update(kw)
    return base


def _inbox(tmp_path, *recs, raw=None):
    done = tmp_path / "inbox" / "done"
    done.mkdir(parents=True, exist_ok=True)
    for r in recs:
        (done / f"{r['id']}-{r.get('attempt', 0)}.json").write_text(json.dumps(r))
    for name, text in (raw or {}).items():
        (done / name).write_text(text)
    return tmp_path / "inbox"


def _one(rec, rate=None):
    parsed, reason = freebuff.parse_record(rec, rate)
    assert reason is None, reason
    return parsed[1]


def test_rate_defaults_and_pool_rate():
    r = FreebuffRate()
    assert (
        r.monthly_fee_usd,
        r.pool_hours_per_day,
        r.wallet_usd_per_hour,
        r.days_per_month,
    ) == (8.0, 50, 0.05, 30)
    assert r.pool_usd_per_hour == pytest.approx(0.0053333333)
    assert FreebuffRate(
        monthly_fee_usd=15, pool_hours_per_day=10, days_per_month=30
    ).pool_usd_per_hour == pytest.approx(0.05)
    with pytest.raises(ValueError):
        FreebuffRate(pool_hours_per_day=0)


def test_rate_from_env():
    r = FreebuffRate.from_env(
        {
            "TOKENOMICS_FREEBUFF_MONTHLY_FEE_USD": "12",
            "TOKENOMICS_FREEBUFF_DAYS_PER_MONTH": "31",
        }
    )
    assert (r.monthly_fee_usd, r.days_per_month, r.pool_hours_per_day) == (
        12.0,
        31.0,
        50,
    )
    with pytest.raises(ValueError, match="MONTHLY_FEE_USD"):
        FreebuffRate.from_env({"TOKENOMICS_FREEBUFF_MONTHLY_FEE_USD": "abc"})


def test_worked_example_1x_one_hour():
    e = _one(_rec("a", newSession=True, sessionMultiplier=1))
    assert e.cost_usd == pytest.approx(8 / (50 * 30)) and e.cost_usd == pytest.approx(
        0.0053333333
    )
    assert (e.provider, e.model, e.tokens_in, e.tokens_out, e.calls, e.violation) == (
        "freebuff",
        "fb-model",
        0,
        0,
        1,
        None,
    )
    assert e.ts_utc == "2026-10-29T12:00:00+00:00"


def test_worked_example_3x_is_three_hours():
    assert _one(
        _rec("a", newSession=True, sessionMultiplier=3)
    ).cost_usd == pytest.approx(3 * 8 / (50 * 30))  # 0.016


def test_worked_example_wallet_paid():
    e = _one(_rec("a", newSession=True, sessionMultiplier=2, paidFrom="wallet"))
    assert e.cost_usd == pytest.approx(2 * 0.05)  # 0.10, wallet rate not pool rate
    pool = _one(_rec("a", newSession=True, sessionMultiplier=2, paidFrom="pool"))
    assert pool.cost_usd == pytest.approx(2 * POOL)


def test_balance_difference_wins_over_multiplier():
    # session minimum: the balance moved by 0.25h even though the multiplier says 3x
    rec = _rec(
        "a",
        newSession=True,
        sessionMultiplier=3,
        balanceBefore={"spendableHours": 10.0},
        balanceAfter={"spendableHours": 9.75},
    )
    assert _one(rec).cost_usd == pytest.approx(0.25 * POOL)
    wallet = dict(rec, paidFrom="wallet")
    assert _one(wallet).cost_usd == pytest.approx(0.25 * 0.05)


def test_equal_balances_mean_no_charge_not_multiplier():
    rec = _rec(
        "a",
        newSession=True,
        sessionMultiplier=3,
        balanceBefore={"spendableHours": 5},
        balanceAfter={"spendableHours": 5},
    )
    assert _one(rec).cost_usd == 0.0


def test_balance_increase_is_not_a_charge_falls_back():
    refill = _rec(
        "a",
        newSession=True,
        sessionMultiplier=2,
        balanceBefore={"spendableHours": 5},
        balanceAfter={"spendableHours": 8},
    )
    assert _one(refill).cost_usd == pytest.approx(
        2 * POOL
    )  # before < after -> newSession rule
    assert _one(dict(refill, newSession=False)).cost_usd == 0.0


@pytest.mark.parametrize(
    "rec",
    [
        _rec("a"),  # nothing at all
        _rec(
            "a", balanceBefore={}, balanceAfter={"spendableHours": 3}
        ),  # one side missing
        _rec(
            "a",
            balanceBefore={"spendableHours": "x"},
            balanceAfter={"spendableHours": 3},
        ),  # junk
        _rec("a", balanceBefore=7, balanceAfter=None),  # wrong types
        _rec(
            "a", newSession=False, sessionMultiplier=3
        ),  # existing session: no new charge
    ],
)
def test_missing_balance_fields_means_no_charge(rec):
    assert _one(rec).cost_usd == 0.0


def test_new_session_without_multiplier_is_one_hour():
    assert _one(_rec("a", newSession=True)).cost_usd == pytest.approx(POOL)
    assert _one(
        _rec("a", newSession=True, sessionMultiplier="fast")
    ).cost_usd == pytest.approx(POOL)


def test_custom_rate_and_unknown_keys_and_model_fallback():
    rate = FreebuffRate(
        monthly_fee_usd=30, pool_hours_per_day=10, days_per_month=30
    )  # 0.10/h
    rec = {
        "id": "a",
        "completedAt": 1_793_275_200,
        "newSession": True,
        "from": "x",
        "surprise": [1],
        "resumeOf": None,
    }
    e = _one(rec, rate)
    assert e.cost_usd == pytest.approx(0.10) and e.model == "unknown"
    assert e.ts_utc == "2026-10-29T12:00:00+00:00"  # epoch seconds accepted
    assert (
        _one(dict(rec, completedAt=1_793_275_200_000)).ts_utc == e.ts_utc
    )  # and milliseconds


def test_session_ended_without_charge_is_skipped_but_with_charge_is_kept():
    assert freebuff.parse_record(_rec("a", outcome="session_ended")) == (
        None,
        "session_ended",
    )
    charged = _rec(
        "a",
        outcome="session_ended",
        balanceBefore={"spendableHours": 2},
        balanceAfter={"spendableHours": 1},
    )
    assert _one(charged).cost_usd == pytest.approx(POOL)
    assert (
        _one(_rec("a", outcome="completed")).cost_usd == 0.0
    )  # other zero-charge outcomes stay (a call)


def test_required_fields_and_garbage():
    assert freebuff.parse_record({"completedAt": "2026-10-29T12:00:00Z"}) == (
        None,
        "malformed",
    )
    assert freebuff.parse_record(["x"]) == (None, "malformed")
    assert freebuff.parse_record({"id": "a"}) == (None, "bad_ts")
    assert freebuff.parse_record({"id": "a", "completedAt": "soon"}) == (None, "bad_ts")


def test_ingest_directory_skips_bad_files_and_ignores_other_dirs(tmp_path):
    inbox = _inbox(
        tmp_path,
        _rec("a", newSession=True, sessionMultiplier=1),
        _rec("b", outcome="session_ended"),
        raw={
            "torn.json": '{"id": "c", "completedAt": "2026-10',
            "empty.json": "",
            "list.json": "[]",
            "notes.txt": "ignored",
            "noid.json": '{"completedAt": "2026-10-29T12:00:00Z"}',
        },
    )
    (inbox / "pending").mkdir()
    (inbox / "pending" / "p.json").write_text(
        json.dumps(_rec("p", newSession=True))
    )  # not done/ -> ignored
    led = Ledger(tmp_path / "ledger.jsonl")
    res = freebuff.ingest(inbox, led, rate=FreebuffRate())
    assert res.appended == 1
    assert res.skipped == {"malformed": 4, "session_ended": 1}
    (e,) = led.entries()
    assert e.cost_usd == pytest.approx(POOL)


def test_empty_or_missing_done_dir_is_fine_missing_inbox_is_an_error(tmp_path):
    (tmp_path / "inbox").mkdir()
    assert freebuff.ingest(tmp_path / "inbox", tmp_path / "l.jsonl").appended == 0
    with pytest.raises(FileNotFoundError):
        freebuff.ingest(tmp_path / "nope", tmp_path / "l.jsonl")


def test_double_run_is_idempotent_and_picks_up_new_records_and_attempts(tmp_path):
    inbox = _inbox(
        tmp_path,
        _rec("a", newSession=True, sessionMultiplier=1),
        _rec("b", newSession=True),
    )
    led = Ledger(tmp_path / "ledger.jsonl")
    assert freebuff.ingest(inbox, led).appended == 2
    before = led.path.read_bytes()
    assert freebuff.ingest(inbox, led).appended == 0
    assert led.path.read_bytes() == before
    _inbox(tmp_path, _rec("c", newSession=True))
    assert freebuff.ingest(inbox, led).appended == 1
    # a retry of the same id is a distinct (id, attempt) and is billed on its own
    _inbox(tmp_path, _rec("a", attempt=1, resumeOf="a", newSession=True))
    assert freebuff.ingest(inbox, led).appended == 1
    assert freebuff.ingest(inbox, led).appended == 0
    assert len(led.entries()) == 4


def test_half_written_file_is_retried_not_lost(tmp_path):
    inbox = _inbox(tmp_path, raw={"x.json": '{"id": "x", "complet'})
    led = Ledger(tmp_path / "ledger.jsonl")
    assert freebuff.ingest(inbox, led).skipped == {"malformed": 1}
    (inbox / "done" / "x.json").write_text(json.dumps(_rec("x", newSession=True)))
    assert freebuff.ingest(inbox, led).appended == 1


def test_since_filters_but_marks_seen(tmp_path):
    inbox = _inbox(
        tmp_path,
        _rec("old", completedAt="2026-10-01T00:00:00Z", newSession=True),
        _rec("new", completedAt="2026-10-20T00:00:00Z", newSession=True),
    )
    led = Ledger(tmp_path / "ledger.jsonl")
    res = freebuff.ingest(inbox, led, since="2026-10-10")
    assert res.appended == 1 and res.skipped == {"since": 1}
    assert freebuff.ingest(inbox, led).appended == 0 and len(led.entries()) == 1


def test_state_sidecar_atomic_and_corrupt_state_raises(tmp_path):
    inbox = _inbox(tmp_path, _rec("a", newSession=True))
    led = Ledger(tmp_path / "ledger.jsonl")
    freebuff.ingest(inbox, led)
    sp = tmp_path / "ledger.jsonl.freebuff.state.json"
    assert json.loads(sp.read_text())["seen"] == ["a#0"]
    assert not list(tmp_path.glob("*.tmp"))
    sp.write_text("{")
    with pytest.raises(ValueError, match="corrupt ingest state"):
        freebuff.ingest(inbox, led)
    assert len(led.entries()) == 1


class _Crash(Exception):
    pass


@pytest.mark.parametrize("nth, rows_after_crash", [(1, 0), (2, 2)])
def test_crash_between_append_and_state_write(
    tmp_path, monkeypatch, nth, rows_after_crash
):
    inbox = _inbox(tmp_path, _rec("a", newSession=True), _rec("b", newSession=True))
    led = Ledger(tmp_path / "ledger.jsonl")
    real, calls = _ingest.write_state, []

    def flaky(path, state):
        calls.append(1)
        if len(calls) == nth:
            raise _Crash()
        real(path, state)

    with monkeypatch.context() as m:
        m.setattr(_ingest, "write_state", flaky)
        with pytest.raises(_Crash):
            freebuff.ingest(inbox, led)
    assert len(led.entries()) == rows_after_crash
    freebuff.ingest(inbox, led)
    assert len(led.entries()) == 2  # no loss, no duplicate
    assert freebuff.ingest(inbox, led).appended == 0 and len(led.entries()) == 2


def test_torn_append_is_completed(tmp_path, monkeypatch):
    inbox = _inbox(tmp_path, _rec("a", newSession=True), _rec("b", newSession=True))
    led = Ledger(tmp_path / "ledger.jsonl")
    real = _ingest._append_bytes

    def torn(path, data):
        real(path, data[: len(data) - 7])
        raise _Crash()

    with monkeypatch.context() as m:
        m.setattr(_ingest, "_append_bytes", torn)
        with pytest.raises(_Crash):
            freebuff.ingest(inbox, led)
    freebuff.ingest(inbox, led)
    bad = []
    assert (
        len(led.entries(on_malformed=lambda n, raw: bad.append(n))) == 2 and bad == []
    )


def test_ledger_round_trip_with_the_repos_reader(tmp_path):
    inbox = _inbox(
        tmp_path,
        _rec("a", newSession=True, sessionMultiplier=3),
        _rec(
            "b",
            completedAt="2026-10-30T00:00:00Z",
            newSession=True,
            paidFrom="wallet",
            model="m2",
        ),
    )
    led = Ledger(tmp_path / "ledger.jsonl")
    freebuff.ingest(inbox, led)
    bad = []
    rows = led.entries(on_malformed=lambda n, raw: bad.append(n))
    assert bad == [] and [r.model for r in rows] == [
        "fb-model",
        "m2",
    ]  # ordered by completion time
    assert rows[0].cost_usd == pytest.approx(0.016) and rows[
        1
    ].cost_usd == pytest.approx(0.05)
    assert led.by_model()["m2"].calls == 1
    assert set(json.loads(led.path.read_text().splitlines()[0])) == {
        "ts_utc",
        "provider",
        "model",
        "tokens_in",
        "tokens_out",
        "cost_usd",
    }


def test_commands_ingest_uses_env_inbox_and_rate(tmp_path, monkeypatch):
    inbox = _inbox(tmp_path, _rec("a", newSession=True))
    monkeypatch.setenv(freebuff.ENV_SOURCE, str(inbox))
    monkeypatch.setenv("TOKENOMICS_FREEBUFF_MONTHLY_FEE_USD", "15")  # 15 / 1500 = 0.01
    ledger = str(tmp_path / "ledger.jsonl")
    assert commands.ingest("freebuff", ledger) == 1
    assert commands.ingest("freebuff", ledger) == 0
    assert Ledger(ledger).entries()[0].cost_usd == pytest.approx(0.01)
    monkeypatch.delenv(freebuff.ENV_SOURCE)
    with pytest.raises(ValueError, match=freebuff.ENV_SOURCE):
        commands.ingest("freebuff", ledger)
