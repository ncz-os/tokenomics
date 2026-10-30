"""osrun ingestion adapter — a JSONL ledger written by a budget-capped agent runner.

Each line is one JSON object. Two shapes appear:

* **pass record** — one model pass, the actual spend::

    {"task": "<id>", "pass": "pass1", "model": "claude-opus-5-5", "usd": 0.046593,
     "tokens": {"total": 45588, "input": 2, "output": 1241, "reasoning": 48,
                "cache": {"write": 671, "read": 43626}}, "ts": "<ISO>"}

* **summary record** — ``{"task": "<id>", "summary": {...}}``, a rollup of that
  task's passes. Summaries, and any line without ``usd``, are skipped and counted;
  only the per-pass records are spend, so nothing is ever counted twice.

Mapping onto the canonical ledger row:

* ``provider`` — ``"anthropic"`` when the model name starts with ``claude``, else
  ``"unknown"``.
* ``tokens_in`` = ``input + cache.write + cache.read``. Chosen so ``tokens_in``
  is *all prompt-side tokens the pass processed* (fresh + cache-written +
  cache-read) and ``tokens_in + tokens_out == tokens.total``; counting only the
  fresh ``input`` would report 2 tokens for a 45k-token pass.
* ``tokens_out`` = ``output + reasoning``.
* ``cost_usd`` = ``usd`` — host-authoritative, no pricing-catalog estimate.
* ``ts_utc`` — ``ts`` normalized to RFC3339 UTC. ``calls`` = 1.

No ``tag``/``task`` field is written: they are not part of the ledger schema.

Tolerated: blank/malformed lines (skipped, counted), a truncated last line (left
unconsumed so it is picked up once the runner finishes writing it), unknown
extra keys, a missing ``tokens`` or ``cache`` block.

Idempotence: :func:`ingest` keeps a high-water mark (byte offset plus a digest of
the last consumed line) in a sidecar ``<ledger>.osrun.state.json`` written
atomically; see :mod:`tokenomics_core.adapters._ingest` for the crash-safe
append/state ordering. If the source file is replaced or rewritten (the last
consumed line is no longer at the stored offset) ingestion resumes after the last
line with the same digest, or from the start when it is gone.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
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

HOST = "osrun"
#: Env var naming the runner's JSONL ledger (no built-in default path).
ENV_SOURCE = "TOKENOMICS_OSRUN_LEDGER"


def _is_num(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _tok(d, key: str) -> int:
    v = d.get(key) if isinstance(d, dict) else None
    return max(0, int(v)) if _is_num(v) and math.isfinite(v) else 0


def parse_line(raw: str) -> tuple[LedgerEntry | None, str | None]:
    """Map one line to ``(entry, None)`` or ``(None, skip_reason)``.

    Reasons: ``malformed`` (not a JSON object, or invalid ``usd``), ``summary``,
    ``no_usd``, ``bad_ts``.
    """
    try:
        obj = json.loads(raw)
    except ValueError:
        return None, "malformed"
    if not isinstance(obj, dict):
        return None, "malformed"
    if "summary" in obj and "pass" not in obj:
        return None, "summary"
    if "usd" not in obj or obj["usd"] is None:
        return None, "no_usd"
    usd = obj["usd"]
    if not _is_num(usd) or not math.isfinite(usd) or usd < 0:
        return None, "malformed"
    ts = parse_ts(obj.get("ts"))
    if ts is None:
        return None, "bad_ts"
    tokens = obj.get("tokens") if isinstance(obj.get("tokens"), dict) else {}
    cache = tokens.get("cache") if isinstance(tokens.get("cache"), dict) else {}
    model = (
        obj.get("model")
        if isinstance(obj.get("model"), str) and obj.get("model")
        else "unknown"
    )
    return LedgerEntry(
        ts_utc=ts.isoformat(),
        provider="anthropic" if model.startswith("claude") else "unknown",
        model=model,
        tokens_in=_tok(tokens, "input") + _tok(cache, "write") + _tok(cache, "read"),
        tokens_out=_tok(tokens, "output") + _tok(tokens, "reasoning"),
        cost_usd=float(usd),
    ), None


def _digest(line: bytes) -> str:
    return hashlib.sha1(line.strip()).hexdigest()


def _lines(data: bytes, base: int = 0):
    """Yield ``(start, end, raw, terminated)`` for each line of ``data``."""
    pos = 0
    while pos < len(data):
        nl = data.find(b"\n", pos)
        if nl == -1:
            yield base + pos, base + len(data), data[pos:], False
            return
        yield base + pos, base + nl + 1, data[pos:nl], True
        pos = nl + 1


def _resume(data: bytes, state: dict) -> tuple[int, int | None, str | None]:
    """High-water ``(offset, last_line_start, last_line_digest)`` to resume from."""
    off = state.get("offset") or 0
    sha, start = state.get("last_sha"), state.get("last_start")
    if sha is None or start is None:
        return (off if off <= len(data) else 0), None, None
    if off <= len(data) and _digest(data[start:off]) == sha:
        return off, start, sha
    found: tuple[int, int | None, str | None] = (0, None, None)
    for s, end, raw, _t in _lines(
        data
    ):  # source replaced/rewritten: resume after the last line we recognize
        if _digest(raw) == sha:
            found = (end, s, sha)
    return found


def scan(path: str | os.PathLike, state: dict | None = None):
    """Read lines past the high-water mark in ``state`` (``None`` = from the start).

    Returns ``(entries, marks, skipped)``: new ledger entries in file order, the
    advanced high-water ``marks`` (``offset``, ``last_start``, ``last_sha``) and a
    ``{reason: count}`` of skipped lines. Pure: nothing is written.
    """
    data = Path(path).read_bytes()
    offset, last_start, last_sha = _resume(data, state or {})
    entries: list[LedgerEntry] = []
    skipped: dict[str, int] = {}
    for lstart, lend, raw, terminated in _lines(data[offset:], base=offset):
        if not raw.strip():
            offset = lend
            continue
        try:
            entry, reason = parse_line(raw.decode("utf-8"))
        except UnicodeDecodeError:
            entry, reason = None, "malformed"
        if reason == "malformed" and not terminated:
            skipped["truncated"] = skipped.get("truncated", 0) + 1
            break  # leave it unconsumed: the runner may still be writing it
        if entry is not None:
            entries.append(entry)
        else:
            skipped[reason] = skipped.get(reason, 0) + 1
        offset, last_start, last_sha = lend, lstart, _digest(raw)
    marks = {"offset": offset, "last_start": last_start, "last_sha": last_sha}
    return entries, marks, skipped


def resolve_source(path: str | os.PathLike | None = None) -> Path:
    """``path`` or ``$TOKENOMICS_OSRUN_LEDGER``; there is no built-in default."""
    path = path or os.environ.get(ENV_SOURCE)
    if not path:
        raise ValueError(f"osrun source not set: pass a path or set {ENV_SOURCE}")
    if not Path(path).is_file():
        raise FileNotFoundError(f"osrun ledger not found: {path}")
    return Path(path)


def ingest(
    path: str | os.PathLike, ledger: Ledger | str | os.PathLike, *, since=None
) -> IngestResult:
    """Append the runner's not-yet-ingested pass records to ``ledger``.

    ``since`` (datetime or ISO string) drops passes older than that instant; they still advance the
    high-water mark so they are not reconsidered. Re-running appends nothing new.
    Single writer per ledger is assumed.
    """
    path = resolve_source(path)
    led = as_ledger(ledger)
    spath = state_path(led, HOST)
    state = load_state(spath)
    result = IngestResult(recovered=recover(led, spath, state))
    if result.recovered:
        state = load_state(spath)
    entries, marks, skipped = scan(path, state)
    result.skipped.update(skipped)
    cutoff = coerce_since(since)
    if cutoff is not None:
        kept = [e for e in entries if parse_ts(e.ts_utc) >= cutoff]
        if len(kept) != len(entries):
            result.skipped["since"] = len(entries) - len(kept)
        entries = kept
    commit(led, spath, state, entries, marks)
    result.appended = len(entries)
    return result
