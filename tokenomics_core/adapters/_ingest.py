"""Shared plumbing for *file-reading* adapters (osrun, freebuff): the sidecar
state file, crash-safe batch commit, and timestamp normalization.

Commit protocol (single writer; exactly-once for the rows of one batch)::

    1. write sidecar  {marks: OLD, pending: {before, block, marks: NEW}}   (atomic)
    2. append `block` to the ledger and fsync
    3. write sidecar  {marks: NEW, pending: null}                          (atomic)

The intent record (step 1) carries the exact bytes to append and the ledger
size before the append, so a crash at any point is repaired on the next run by
:func:`recover` instead of losing or duplicating rows:

* crash before step 2  -> ledger still ends at ``before``: the block is appended.
* crash mid step 2     -> ledger holds a prefix of the block: the rest is appended.
* crash before step 3  -> ledger already holds the whole block: nothing appended,
  the new marks are just committed.

If the ledger no longer matches at ``before`` (someone else rewrote it), the
block is appended in full: at-least-once is preferred to silently dropping spend.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from tokenomics_core.ledger import Ledger, LedgerEntry

STATE_VERSION = 1


@dataclass
class IngestResult:
    """Outcome of one ``ingest`` call."""

    appended: int = 0
    #: Rows of a previously interrupted batch that this run completed/committed.
    recovered: int = 0
    #: Skipped inputs by reason (e.g. ``{"summary": 2, "malformed": 1}``).
    skipped: dict[str, int] = field(default_factory=dict)

    def skip(self, reason: str) -> None:
        self.skipped[reason] = self.skipped.get(reason, 0) + 1

    @property
    def skipped_total(self) -> int:
        return sum(self.skipped.values())


def as_ledger(ledger: Ledger | str | os.PathLike) -> Ledger:
    return ledger if isinstance(ledger, Ledger) else Ledger(ledger)


def state_path(ledger: Ledger, host: str) -> Path:
    """Sidecar next to the ledger: ``<ledger>.<host>.state.json``."""
    return Path(f"{ledger.path}.{host}.state.json")


def load_state(path: Path) -> dict:
    """Missing sidecar -> fresh state. A corrupt one raises: silently starting
    over would re-ingest (duplicate) the whole source."""
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {"version": STATE_VERSION}
    try:
        st = json.loads(raw)
        if not isinstance(st, dict):
            raise ValueError("not an object")  # noqa: TRY004 - same corrupt-state error as bad JSON
    except ValueError as e:
        raise ValueError(f"corrupt ingest state {path}: {e}") from e
    return st


def write_state(path: Path, state: dict) -> None:
    """Atomic: write a temp file in the same directory, fsync, ``os.replace``."""
    state = dict(state, version=STATE_VERSION)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        fh.write(json.dumps(state, sort_keys=True))
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)
    try:  # make the rename itself durable (best effort; not on every platform)
        dfd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)
    except OSError:
        pass


def _append_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("ab") as fh:
        fh.write(data)
        fh.flush()
        os.fsync(fh.fileno())


def _ledger_size(ledger: Ledger) -> int:
    try:
        return ledger.path.stat().st_size
    except FileNotFoundError:
        return 0


def _ends_with_newline(ledger: Ledger) -> bool:
    try:
        with ledger.path.open("rb") as fh:
            fh.seek(-1, os.SEEK_END)
            return fh.read(1) == b"\n"
    except OSError:  # missing or empty
        return True


def recover(ledger: Ledger, spath: Path, state: dict) -> int:
    """Finish an interrupted batch (see module docstring). Returns rows completed."""
    pending = state.get("pending")
    if not pending:
        return 0
    block = pending["block"].encode("utf-8")
    before = int(pending["before"])
    size = _ledger_size(ledger)
    existing = b""
    if size >= before and size > 0:
        with ledger.path.open("rb") as fh:
            fh.seek(before)
            existing = fh.read(len(block))
    if existing == block:
        pass  # append completed; only the state write was lost
    elif size >= before and block.startswith(existing):
        _append_bytes(ledger.path, block[len(existing) :])  # torn append: finish it
    else:
        _append_bytes(ledger.path, block)
    rows = block.count(b"\n") - (1 if block.startswith(b"\n") else 0)
    state = {k: v for k, v in state.items() if k != "pending"}
    state.update(pending["marks"])
    write_state(spath, state)
    return rows


def commit(
    ledger: Ledger, spath: Path, state: dict, entries: list[LedgerEntry], marks: dict
) -> None:
    """Append ``entries`` and advance the high-water ``marks`` (module docstring)."""
    base = {k: v for k, v in state.items() if k != "pending"}
    if not entries:
        if any(base.get(k) != v for k, v in marks.items()):
            write_state(spath, {**base, **marks})
        return
    block = "".join(e.to_line() + "\n" for e in entries)
    if not _ends_with_newline(
        ledger
    ):  # a torn tail from someone else: keep our rows on their own lines
        block = "\n" + block
    write_state(
        spath,
        {
            **base,
            "pending": {"before": _ledger_size(ledger), "block": block, "marks": marks},
        },
    )
    _append_bytes(ledger.path, block.encode("utf-8"))
    write_state(spath, {**base, **marks})


def parse_ts(v) -> datetime | None:
    """ISO-8601/RFC3339 string or epoch number (seconds, or ms when >= 1e11) -> aware UTC
    datetime. Naive strings are taken as UTC. ``None`` when unparseable."""
    try:
        if isinstance(v, bool):
            return None
        if isinstance(v, (int, float)):
            x = float(v)
            return datetime.fromtimestamp(
                x / 1000.0 if abs(x) >= 1e11 else x, tz=timezone.utc
            )
        if isinstance(v, str) and v.strip():
            d = datetime.fromisoformat(v.strip().replace("Z", "+00:00"))
            return (
                d.replace(tzinfo=timezone.utc)
                if d.tzinfo is None
                else d.astimezone(timezone.utc)
            )
    except (ValueError, OverflowError, OSError):
        pass
    return None


def coerce_since(since) -> datetime | None:
    if since is None:
        return None
    if isinstance(since, datetime):
        return (
            since.replace(tzinfo=timezone.utc)
            if since.tzinfo is None
            else since.astimezone(timezone.utc)
        )
    d = parse_ts(since)
    if d is None:
        raise ValueError(f"unparseable since: {since!r}")
    return d
