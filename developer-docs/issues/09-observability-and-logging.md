# Issue 09 — Observability and structured logging

## Summary

The pipeline is long, multi-stage, and has an LLM in the middle. The single most valuable investment in operability is per-block traceability: given a row in the output workbook, or a block that was mis-classified, reconstruct the full journey through stages 1–7 from logs alone. No stack-stepping, no reruns, no peeking at intermediate files — just grep by `block_id`.

## Log-line shape

Every stage emits one structured log line per block it touches. Every line includes the fields below at minimum. Additional stage-specific fields are allowed.

```json
{
  "ts": "2026-04-24T14:22:01.123Z",
  "run_id": "run_a4b1…",
  "fixture_id": "FX-CHOICES-001",
  "stage": "classify",
  "block_id": "blk_p1_c3a2_…",
  "decision": "classified=CheckboxOption",
  "confidence": 0.92,
  "rule_id": "choices.glyph+short_text",
  "latency_ms": 0.8,
  "cost_usd": 0.0,
  "notes": ""
}
```

Stages:
- `extract` — stage 1. One line per block extracted.
- `classify` — stage 2. One line per block classified, with `rule_id`.
- `gate` — stage 3. One line per block; `decision` is `pass_through` or `llm_refine`; `notes` lists `GateReason`s.
- `llm` — stage 4. One line per block the LLM touched. Includes `model_id`, `input_tokens`, `output_tokens`, `cache_hit`.
- `merge` — stage 5. One line per block, with final `provenance.source`.
- `map` — stage 6. One line per block mapped to a row; includes `sequence`.
- `write` — stage 7. One line per row written.

## Sink

JSON lines to stdout by default. In production, forwarded to CloudWatch. In dev, a `tee` to `logs/{run_id}.jsonl` enables post-hoc trace queries:

```
jq 'select(.block_id=="blk_p1_c3a2_…")' logs/{run_id}.jsonl
```

Every stage for that block, in order. That's the deliverable.

## Run-level telemetry

At the end of a run, emit a summary record:

```json
{
  "ts": "...", "run_id": "...", "fixture_id": "...",
  "stage": "summary",
  "blocks_extracted": 412, "blocks_classified": 412,
  "blocks_routed": 68, "route_rate": 0.165,
  "blocks_llm_refined": 68, "blocks_llm_invalid": 0,
  "rows_written": 98,
  "total_latency_ms": 8421, "total_cost_usd": 0.042,
  "cache_hit_rate": 0.0,
  "template_version": "1.0.0+sha:abc123…",
  "pipeline_version": "0.1.0",
  "model_id": "arn:aws:bedrock:us-east-1::foundation-model/amazon.nova-pro-v1:0"
}
```

Eval E aggregates over these summary records.

## PII discipline (cross-ref issue 11)

- **Never** log raw block text at `info` level. Log the first 40 chars at `debug`, full text only at `trace`.
- Client names, SSNs, DOBs in `Question Text` values are *template placeholders* in v1 fixtures but *real values* in production. The same log shape is used in both; the level gate is what protects PII.
- `rationale` strings from the LLM may quote block text — redact at the LLM client boundary before logging.

## Known failure modes

1. **Log volume.** One line per block × ~400 blocks × 7 stages = ~2800 lines per PDF. Manageable; verify with log ingestion cost at scale.
2. **`block_id` stability across stages.** Every stage MUST carry `block_id` from stage 1. Easy bug if a stage generates its own ID.
3. **Run-id propagation.** Set at entry, threaded through via context var or explicit arg. If a stage forgets, its lines become un-joinable.
4. **Cost field accuracy.** Populate only at stage 4 and only after Bedrock returns token counts. `null` everywhere else, not `0`.

## Acceptance criteria

- For any `block_id` in any run, `jq 'select(.block_id==…)' logs/{run_id}.jsonl` returns a chronological trace across at least stages 1, 2, 3, 5, 6 (and 4, 7 where applicable).
- Run summary captures every field Eval E requires.
- No raw block text at `info` level (audited via a lint check on the log schema).

## Cross-references

- Used by: every issue.
- Downstream eval: `evals/eval-E-cost-latency.md`
- Security: `11-security-pii-handling.md`
