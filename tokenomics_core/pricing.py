"""Model pricing catalog. Rates are realized ``$ / 1M tokens`` in the public
price-list shape. The catalog drives the cost-counterfactual / avoided-spend
headline; per-call cost in the ledger is the truth for what was actually paid.

On-disk JSON shape matches the Rust ``PricingCatalog`` and TS catalog (snake_case)
so a catalog produced by any of the three tools loads unchanged.
"""

from __future__ import annotations

import json
import math
import os
import stat as statmod
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

# Largest pricing catalog accepted (2 MiB); larger files are rejected unread.
MAX_PRICE_BYTES = 2_097_152

_NUMERIC_PRICE_FIELDS = (
    "usd_per_mtok",
    "input_usd_per_mtok",
    "output_usd_per_mtok",
    "cache_read_usd_per_mtok",
    "cache_write_usd_per_mtok",
    "reasoning_usd_per_mtok",
)


def validate_model_price(model_id: str, raw: object) -> tuple[bool, list[str]]:
    """Validate a model price entry. Returns ``(valid, warnings)`` and mutates
    ``raw`` in place, dropping invalid fields (mirrors the TS ``validateModelPrice``)."""
    warnings: list[str] = []
    if not isinstance(raw, dict):
        return False, [f'model "{model_id}": value is not an object, skipping']
    for f in _NUMERIC_PRICE_FIELDS:
        v = raw.get(f)
        if v is None:
            continue
        is_number = isinstance(v, (int, float)) and not isinstance(v, bool)
        # A finite, non-negative number is a real rate — keep it.
        if is_number and math.isfinite(v) and v >= 0:
            continue
        # A finite negative value is the catalog's "unpriced / unknown" sentinel
        # (e.g. -1000000). Drop the field silently — it is intentional, not
        # malformed, so it must not spam a warning on every report.
        if is_number and math.isfinite(v):
            raw.pop(f, None)
            continue
        # Genuinely malformed (bool, non-number, NaN, Infinity): warn and drop.
        warnings.append(
            f'model "{model_id}": {f}={json.dumps(v) if v == v else "NaN"} '
            "is not a finite number, skipping field"
        )
        raw.pop(f, None)
    if raw.get("source") is not None and not isinstance(raw.get("source"), str):
        warnings.append(f'model "{model_id}": source is not a string, skipping field')
        raw.pop("source", None)
    has_rate = any(
        isinstance(raw.get(f), (int, float)) and not isinstance(raw.get(f), bool)
        for f in _NUMERIC_PRICE_FIELDS
    )
    if not has_rate:
        warnings.append(f'model "{model_id}": no valid rate fields remain, skipping model')
        return False, warnings
    return True, warnings


@dataclass
class ModelPrice:
    usd_per_mtok: Optional[float] = None
    input_usd_per_mtok: Optional[float] = None
    output_usd_per_mtok: Optional[float] = None
    cache_read_usd_per_mtok: Optional[float] = None
    cache_write_usd_per_mtok: Optional[float] = None
    reasoning_usd_per_mtok: Optional[float] = None
    source: Optional[str] = None

    def is_priced(self) -> bool:
        return (
            (self.usd_per_mtok or 0) > 0
            or (self.input_usd_per_mtok or 0) > 0
            or (self.output_usd_per_mtok or 0) > 0
        )

    def cost_io(self, tokens_in: int, tokens_out: int) -> float:
        in_rate = self.input_usd_per_mtok or 0
        out_rate = self.output_usd_per_mtok or 0
        if in_rate > 0 or out_rate > 0:
            return (tokens_in * in_rate + tokens_out * out_rate) / 1_000_000
        return ((self.usd_per_mtok or 0) * (tokens_in + tokens_out)) / 1_000_000

    @staticmethod
    def from_json(j: dict) -> "ModelPrice":
        return ModelPrice(
            usd_per_mtok=j.get("usd_per_mtok"),
            input_usd_per_mtok=j.get("input_usd_per_mtok"),
            output_usd_per_mtok=j.get("output_usd_per_mtok"),
            cache_read_usd_per_mtok=j.get("cache_read_usd_per_mtok"),
            cache_write_usd_per_mtok=j.get("cache_write_usd_per_mtok"),
            reasoning_usd_per_mtok=j.get("reasoning_usd_per_mtok"),
            source=j.get("source"),
        )

    def to_json(self) -> dict:
        return {k: v for k, v in {
            "usd_per_mtok": self.usd_per_mtok,
            "input_usd_per_mtok": self.input_usd_per_mtok,
            "output_usd_per_mtok": self.output_usd_per_mtok,
            "cache_read_usd_per_mtok": self.cache_read_usd_per_mtok,
            "cache_write_usd_per_mtok": self.cache_write_usd_per_mtok,
            "reasoning_usd_per_mtok": self.reasoning_usd_per_mtok,
            "source": self.source,
        }.items() if v is not None}


class PricingCatalog:
    def __init__(self) -> None:
        self.generated = ""
        self.window = ""
        self.models: dict[str, ModelPrice] = {}
        self.baseline_usd_per_mtok = 0.0
        self.baseline_model = ""

    @staticmethod
    def load(
        path: str | os.PathLike,
        *,
        posix: Optional[bool] = None,
        logger=None,
    ) -> "PricingCatalog":
        """Load, or an empty catalog if absent/corrupt/insecure/oversized (never fatal).

        ``pricing.json`` is treated as trusted input: the size is gated before any
        descriptor is opened; on POSIX the file must be a regular file owned by the
        process and not group/world-writable; each model entry is validated. Pass
        ``posix=False`` to skip the POSIX mode/uid checks; pass ``logger`` to route
        warnings somewhere other than stderr.
        """
        cat = PricingCatalog()
        is_posix = (os.name == "posix") if posix is None else posix

        def warn(msg: str) -> None:
            (logger or sys.stderr.write)(msg)

        # size gate (before any descriptor is opened)
        try:
            st = os.stat(path)
        except OSError as err:
            warn(
                f"tokenomics: warning: pricing catalog {path} rejected — "
                f"cannot stat: {err}; using empty\n"
            )
            return cat
        if not statmod.S_ISREG(st.st_mode):
            warn(
                f"tokenomics: warning: pricing catalog {path} rejected — "
                "not a regular file; using empty\n"
            )
            return cat
        if st.st_size > MAX_PRICE_BYTES:
            warn(
                f"tokenomics: warning: pricing catalog {path} rejected — "
                f"{st.st_size} bytes exceeds {MAX_PRICE_BYTES} limit; using empty\n"
            )
            return cat

        # descriptor-backed permission check + read (no TOCTOU between check and read)
        try:
            fd = os.open(path, os.O_RDONLY)
        except OSError:
            return cat
        try:
            fst = os.fstat(fd)
            if is_posix:
                if fst.st_mode & 0o022:  # S_IWGRP | S_IWOTH
                    mode_str = format(fst.st_mode & 0o777, "03o")
                    warn(
                        f"tokenomics: warning: pricing catalog {path} rejected — "
                        f"insecure mode 0o{mode_str} (must not be group- or "
                        "world-writable); using empty\n"
                    )
                    return cat
                try:
                    proc_uid = os.getuid()
                except AttributeError:
                    proc_uid = None
                if proc_uid is not None and fst.st_uid != proc_uid:
                    warn(
                        f"tokenomics: warning: pricing catalog {path} rejected — "
                        f"owned by uid {fst.st_uid}, process is uid {proc_uid}; using empty\n"
                    )
                    return cat
            chunks: list[bytes] = []
            while True:
                b = os.read(fd, 65_536)
                if not b:
                    break
                chunks.append(b)
            raw = b"".join(chunks).decode("utf-8")
        except OSError:
            return cat
        finally:
            os.close(fd)

        try:
            j = json.loads(raw)
            cat.generated = j.get("generated", "") if isinstance(j.get("generated"), str) else ""
            cat.window = j.get("window", "") if isinstance(j.get("window"), str) else ""
            base = j.get("baseline_usd_per_mtok", 0.0)
            cat.baseline_usd_per_mtok = (
                base if isinstance(base, (int, float)) and not isinstance(base, bool) and base == base else 0.0
            )
            cat.baseline_model = j.get("baseline_model", "") if isinstance(j.get("baseline_model"), str) else ""
            models = j.get("models")
            if isinstance(models, dict):
                skipped = 0
                for k, v in models.items():
                    valid, warnings = validate_model_price(k, v)
                    if valid:
                        cat.models[k] = ModelPrice.from_json(v)
                    else:
                        skipped += 1
                    for w in warnings:
                        warn(f"tokenomics: warning: {w}\n")
                if skipped > 0:
                    plural = "y" if skipped == 1 else "ies"
                    warn(
                        f"tokenomics: warning: skipped {skipped} malformed model entr{plural} in {path}\n"
                    )
        except ValueError as e:
            warn(
                f"tokenomics: warning: pricing catalog {path} unreadable ({e}); using empty\n"
            )
        return cat

    def to_json(self) -> dict:
        return {
            "generated": self.generated,
            "window": self.window,
            "models": {k: v.to_json() for k, v in self.models.items()},
            "baseline_usd_per_mtok": self.baseline_usd_per_mtok,
            "baseline_model": self.baseline_model,
        }

    def save(self, path: str | os.PathLike) -> None:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(p.suffix + ".tmp")
        tmp.write_text(json.dumps(self.to_json(), indent=2) + "\n", encoding="utf-8")
        tmp.replace(p)

    def lookup(self, model: str) -> Optional[ModelPrice]:
        """Tolerant lookup: exact, then case-insensitive, then leaf/suffix match."""
        exact = self.models.get(model)
        if exact:
            return exact
        ml = model.lower()
        leaf = ml.rsplit("/", 1)[-1] if "/" in ml else ml
        for k, v in self.models.items():
            kl = k.lower()
            if kl == ml or kl == leaf or ml.endswith(kl) or kl.endswith(leaf):
                return v
        return None

    def cost(self, model: str, tokens_in: int, tokens_out: int) -> float:
        """Chargeback for a call. Unknown/unpriced model -> $0."""
        p = self.lookup(model)
        return p.cost_io(tokens_in, tokens_out) if p else 0.0

    def is_billed(self, model: str) -> bool:
        p = self.lookup(model)
        return p.is_priced() if p else False

    def avoided(self, tokens: int) -> float:
        """Avoided spend: ``tokens`` priced at the frontier baseline."""
        return (self.baseline_usd_per_mtok * tokens) / 1_000_000
