# Issue 10 — Cost and latency budget

## Summary

The pipeline has one expensive stage (LLM refinement) and six cheap stages. This issue declares the per-PDF budget for v1, breaks it down by stage, identifies the levers that move the budget, and defines what triggers an alert. v2 must declare a comparable budget and meet or beat it (Eval E).

## v1 budget targets

Per-PDF, on the seed fixtures (~10 pages, ~400 blocks):

| Stage | Latency target | Cost target | Notes |
|---|---|---|---|
| 1. Extraction (Docling local) | 5–15s | $0 (local compute) | First page ~2s warm, scales linearly |
| 2. Rule classification | < 200ms | $0 | Pure Python |
| 3. Gate | < 50ms | $0 | Graph + threshold |
| 4. LLM refinement (Nova Pro) | 5–30s | **$0.02–$0.10** | Dominant cost; depends on route rate |
| 5. Merge | < 100ms | $0 | |
| 6. Mapping | < 200ms | $0 | |
| 7. Excel write | < 5s | $0 | I/O bound |
| **Total** | **< 60s** | **< $0.15** | |

Cost ceiling per PDF: **$0.25** (alert threshold). Above this, the run completes but flags the document for review.

## Where the cost goes

Stage 4 dominates. Cost = `routed_block_count × (input_tokens + output_tokens) × per-token_price`.

Levers (in order of impact):

1. **Lower route rate** (issue 03's threshold). Halving route rate halves cost.
2. **Shorter prompts.** Trim few-shot bank, drop redundant context. ~30% gain available.
3. **Cheaper model.** Nova Pro is already the cheapest viable Bedrock model for structured outputs. Falling back to Nova Lite/Micro would save more but the structured-output reliability drops below the 95% Pydantic-validation acceptance bar.
4. **Prompt caching.** Bedrock supports system-prompt caching; a stable system prompt cuts input tokens 50–80% on subsequent calls within a window.
5. **Response caching by prompt-hash.** During eval reruns, hit the cache for free.

Levers (out of scope for v1, candidate for v2):

- Fine-tune a small model on accumulated few-shot bank.
- Replace LLM entirely with a learned classifier on rule features.
- Batch multiple blocks per LLM call.

## Latency budget

End-to-end < 60s on the seed fixtures. Stage 4 is the wall-clock dominator on first-time runs (Bedrock latency is the floor). Concurrency: the LLM client may issue routed-block calls in parallel up to 4-way; Bedrock handles it.

## Alert conditions

- Per-PDF cost > $0.25 → log a `WARN` and tag the run.
- Route rate > 30% → log a `WARN` (likely a novel form needing rule additions).
- LLM-invalid rate > 5% → log a `WARN` (prompt/few-shot drift).
- End-to-end latency > 120s → log a `WARN`.
- Bedrock 5xx rate > 1% over a rolling window → page someone (production only).

Alerts emit to stdout in dev; CloudWatch alarms in production.

## v2 budget contract

Whatever decomposition v2 chooses, it must report `(end_to_end_latency_s, total_cost_usd)` per fixture in the same `summary` log shape (issue 09). Eval E charts v1 vs v2 on the same axes for the same fixtures. v2 may legitimately spend more if it improves Eval B/C scores; the tradeoff is decided by the team, not by the eval.

## Known failure modes

1. **Cold-start Docling.** First run spawns the model; +5–10s. Mitigate with a warm `docling-serve`.
2. **Bedrock throttling.** Cross-account quotas; surface as a WARN with retry-after hint.
3. **Cost-cap surprises** when a novel form routes 50% of blocks. Surface but don't stop — operator should see the failure and patch rules.
4. **Local-cache misses on eval reruns.** Defeats determinism gains. Verify cache path is committed to dev workflow.

## Acceptance criteria

- Both seed fixtures run within budget on the first commit of v1.
- Eval E publishes a per-PDF `(latency, cost)` table.
- Alert conditions fire at the right thresholds (verified by injecting a synthetic high-route fixture).

## Cross-references

- Cost lever: `03-ambiguity-decision-gate.md`
- Cost source: `04-llm-refinement.md`
- Telemetry: `09-observability-and-logging.md`
- Eval: `evals/eval-E-cost-latency.md`
