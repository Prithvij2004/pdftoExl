# Eval E — Cost and latency

## Purpose

Track the per-fixture wall-clock latency and dollar cost of every pipeline run. Not pass/fail; budgets are tracked as trends. v2 declares its own budget and is judged on whether it meets or beats v1's per-fixture numbers.

## Inputs

- The `summary` log record emitted at the end of each pipeline run (issue 09).
- The fixture catalogue (`fixtures.md`).

## Expected outputs

A per-fixture `(latency_s, cost_usd)` table, plus a v1-vs-v2 comparison view when both are available.

## Metrics

### E1. `latency_s`

Wall-clock from PDF read to .xlsx write, in seconds.

### E2. `cost_usd`

Sum of all `cost_usd` log fields across the run. For v1 this is dominated by stage 4 (LLM); other stages contribute zero.

### E3. `cost_per_block_usd` (diagnostic)

```
cost_per_block_usd = total_cost_usd / blocks_extracted
```

Useful for cross-fixture comparison; a 10-page PDF and a 1-page PDF have very different totals but similar per-block costs.

### E4. `route_rate` (v1-specific diagnostic)

```
route_rate = blocks_routed / blocks_extracted
```

Reported for v1 visibility. v2 implementations without a gate may report `0.0` or omit.

### E5. `cache_hit_rate` (eval-time only)

Fraction of LLM calls that hit the prompt-response cache (issue 04). 0 on first run; ~1 on eval reruns. Confirms determinism is intact.

## Budget targets (v1, on seed fixtures)

| Metric | `FX-CHOICES-001` (~10pp) | `FX-TXLTSS-001` (~1pp) |
|---|---|---|
| `latency_s` | < 60 | < 20 |
| `cost_usd` | < 0.15 | < 0.05 |
| `cost_per_block_usd` | < 0.001 | < 0.001 |

These are starting targets. Surface the actual numbers from the first 10 runs and lock the budget at p90 + 25% headroom.

## v1 vs v2 comparison

When v2 is available, the report shows side-by-side per-fixture:

| Fixture | v1 latency | v2 latency | Δ | v1 cost | v2 cost | Δ |
|---|---|---|---|---|---|---|
| FX-CHOICES-001 | 42s | 18s | -24s | $0.08 | $0.00 | -$0.08 |
| FX-TXLTSS-001 | 12s | 9s | -3s | $0.01 | $0.00 | -$0.01 |

A v2 that wins on cost but regresses on Eval C is **not** a win — Evals C and D are the correctness gates; E is the budget gate. Decisions are made by the team looking at all of them together.

## Tooling-agnostic harness contract

```
for each fixture:
    pipeline_summary = run_pipeline(fixture.pdf).summary
    record(fixture.id, pipeline_summary.latency_ms, pipeline_summary.cost_usd, ...)
emit_per_fixture_table()
emit_comparison_to_baseline(if baseline_available)
```

The harness reads `summary` log records. Implementation is a 30-line script in any language.

## Alert conditions

(Operational, not eval-time; replicated here for cross-reference with issue 10.)

- Per-fixture cost > $0.25 in production.
- Per-fixture latency > 120s in production.
- Bedrock 5xx rate > 1% over a rolling 1-hour window.

Eval E reports these conditions when they fire during a CI run, but doesn't fail the build on them — those are operational signals.

## Out of scope

- Correctness (Evals A–D).
- The pipeline's internal stage timings (issue 09's per-stage logs cover that).

## Cross-references

- Budget design: `issues/10-cost-and-latency-budget.md`
- Telemetry: `issues/09-observability-and-logging.md`
- LLM cost source: `issues/04-llm-refinement.md`
