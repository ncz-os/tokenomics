# tokenomics

**Unified LLM-spend ingestion engine + FinOps — one canonical ledger across OpenClaw, Zoder, Hermes, and Goose.**

`tokenomics` is a host-neutral core (ledger → pricing → report → FinOps) plus thin
per-host ingestion adapters. Each host's native usage is mapped into one canonical
ledger row, so spend from different agent runtimes lands in a single report.

> Distributed on PyPI as **`ncz-tokenomics`**; the import package, the `tokenomics`
> command, and the Hermes plugin are all named `tokenomics`.

## Canonical ledger row

```
{ ts_utc, provider, model, tokens_in, tokens_out, cost_usd,
  caller?, task?, tier?, cache_hit_ratio? }   # last four = optional FinOps tags
```

Cost precedence is host-neutral: **host-reported cost → pricing-catalog estimate → $0** (never invented).

## Hosts

| host | surface | cost |
|------|---------|------|
| **Goose** (block) | SQLite session store (`~/.local/share/goose/sessions/sessions.db`) | host-authoritative (`accumulated_cost`) |
| **Hermes** (NousResearch) | SQLite session store (`~/.hermes/state.db`) | host-authoritative (`actual_cost_usd`) → estimate → catalog |
| **OpenClaw** | `model.usage` event bus (shipped plugin) | host cost |
| **Zoder / zeroclaw** | in-process cost-tracker + offline pricing | host cost else catalog |

Goose and Hermes are *pulled* read-only from their existing stores — no host modification.

## Install

```bash
pip install ncz-tokenomics
```

## CLI

```bash
tokenomics ingest --host goose  --ledger ledger.jsonl
tokenomics ingest --host hermes --ledger ledger.jsonl --pricing pricing.json
tokenomics report --ledger ledger.jsonl --days 30
tokenomics finops --ledger ledger.jsonl --days 30
```

## Hermes plugin

Installing the package exposes a Hermes plugin via the `hermes_agent.plugins`
entry point. Enable it in `~/.hermes/config.yaml`:

```yaml
plugins:
  enabled:
    - tokenomics
```

Then, from Hermes's own session store:

```bash
hermes tokenomics            # sync ~/.hermes/state.db -> ledger, print spend report
hermes tokenomics --finops   # FinOps view (allocation / realized-rate / advisor / forecast)
hermes tokenomics --ingest-only
```

Read-only over Hermes's own `state.db`; no Hermes core changes.

## License

Apache-2.0 © Jason Perlow
