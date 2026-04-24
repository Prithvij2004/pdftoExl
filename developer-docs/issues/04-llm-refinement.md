# Issue 04 — Stage 4: LLM refinement on ambiguous blocks

## Summary

Stage 4 receives the residual subset that the gate could not confidently classify, plus enough context to make sense of it (immediate neighbours, parent candidates, the controlled vocabulary, a few-shot example bank). It returns a corrected block type, a parent reference where applicable, and — critically — a Branching Logic expression in the EAB DSL when the block depends on an earlier answer. Output is structured JSON validated by Pydantic; no free text post-processing.

This is the only stage that costs money per call. Every design choice trades latency, cost, and accuracy. The ratio of routed blocks (issue 03) × per-call cost is the dominant term in Eval E.

## Inputs / Outputs

**Input per call:** a batch of `RoutedBlock`s, each with:

```python
class RoutedBlock(BaseModel):
    block: ClassifiedBlock              # what stage 2 produced
    neighbours: List[ClassifiedBlock]   # ±3 in reading order, plus same-page geometric neighbours
    candidate_parents: List[str]        # block_ids of nearby plausible parents
    doc_meta: DocMeta                   # assessment_title, program, page count
    gate_reasons: List[GateReason]      # why this block routed
```

**Output per call:** validated structured response:

```python
class LLMRefinement(BaseModel):
    block_id: str
    block_type: BlockType                            # MUST be in the enum
    confidence: float                                # LLM-asserted; capped at 0.95
    parent_link: Optional[ParentLink]
    branching_logic: Optional[str]                   # EAB DSL, e.g. "Display if Q7 = 'Lives in other's home'"
    rationale: str                                   # short, used for log + eval triage; not surfaced in workbook
```

A response that fails Pydantic validation (wrong enum value, malformed branching syntax) triggers one retry with a sharpened prompt; second failure routes the block to the merge stage with `block_type=Unknown`, surfacing it for human review at Eval B time.

## v1 design decision

- **Provider:** Amazon Bedrock Converse API with structured outputs (JSON-schema-constrained).
- **Primary model:** Amazon Nova Pro — lowest per-token cost on Bedrock that still handles structured-output reliably.
- **Fallback ladder:** if Nova Pro returns invalid JSON twice in a row on the same block, fall through to Mistral Large 2; if that also fails, Llama 3.3 70B; then mark the block `Unknown` and continue.
- **Determinism:** `temperature=0`, `top_p=1`, fixed seed where the provider exposes one. Eval reruns must produce identical outputs given identical inputs.
- **Pydantic** validates both the input prompt-payload schema and the output schema.

## Prompt structure

A compact prompt outperforms a long one for this task. Recommended sections, in this order:

1. **System message** (200–400 tokens, cached): the role ("You classify form blocks from state assessment PDFs into a fixed taxonomy"), the controlled vocabulary, the EAB Branching Logic DSL grammar, the JSON output schema.
2. **Few-shot examples** (3–5 examples, ~100 tokens each): drawn from previously approved fixtures. Selection strategy: see "Few-shot selection" below.
3. **Task instance** (the routed block + its context): block text, neighbours' text, candidate parents, gate reasons.

System message is identical across calls within a run → exploit Bedrock prompt caching. Per-instance overhead is then ~300–600 tokens, well within Nova Pro's sweet spot.

## Few-shot example selection

Three options, in increasing sophistication:

1. **Hand-curated static set.** Pick 5 examples covering the most-routed `(BlockType, GateReason)` pairs. Cheap, predictable, easy to audit. v1 starts here.
2. **BM25 over the example bank.** Score each candidate example against the routed block's text + neighbours. Easy retrofit; no embeddings required.
3. **Embedding-based retrieval.** Best quality but needs an embedding model and an index. Defer to v2.

v1 ships with option 1, with a code path for option 2 behind a feature flag. The example bank lives in `developer-docs/llm/examples.jsonl` (created when v1 starts).

## Branching Logic — what we ask the LLM to produce

The EAB Branching Logic DSL (as observed in the CHOICES reference workbook's `Question Rule` and `Branching Logic` columns):

```
Display if Q<seq> = "<answer text>"
Display if Q<seq> != "<answer text>"
Display if Q<seq> in ("a", "b")
Display if Q<seq1> = "x" AND Q<seq2> = "y"
```

The LLM receives the grammar in its system prompt and is asked to output only one of these forms (or `null`). v1 then validates the output against a regex parser; a failed parse counts as invalid JSON and triggers the retry path.

**Important:** the LLM does *not* know the final `Sequence` value of the parent question — that's assigned in stage 6. v1 has it emit a `parent_link` (block-id reference); stage 6 substitutes the sequence number into the branching expression. This decouples the LLM from sequence assignment.

## Determinism and reproducibility

For Eval A and Eval C reruns to be meaningful, the same input must produce the same output. Steps:

- `temperature=0`, `top_p=1`.
- Pin the `model_id` (full ARN, including version suffix). Bedrock model versions can change semantics; pinning the version is non-negotiable.
- Cache responses by SHA256 of the canonical prompt payload. Eval reruns hit the cache; production runs miss.
- Log the model_id + cache_hit flag per call (issue 09).

Caveat: even at `temperature=0`, Bedrock's hosted infra is not bit-deterministic. Treat 1–2% output variance across reruns as expected; the cache makes it irrelevant for evals.

## Retry / timeout policy

- Per-call timeout: 15s (Nova Pro median is ~1–2s; 15s is generous tail margin).
- On invalid JSON / schema mismatch: 1 retry with the same prompt + an "your previous response was invalid because…" preamble.
- On retry failure: fall through the model ladder (Nova Pro → Mistral Large 2 → Llama 3.3 70B).
- On HTTP 5xx / throttling: exponential backoff, 3 retries, then escalate.
- Total per-block budget: 45s wall-clock. Beyond that, mark `Unknown` and continue.

## Known failure modes

1. **Hallucinated parent IDs.** The LLM invents a `block_id` that doesn't exist. Mitigation: validate against the input's `candidate_parents` list; reject as schema-invalid otherwise.
2. **DSL syntax drift.** The LLM emits `Show when Q7 == "Lives elsewhere"` instead of the EAB form. Regex parser rejects; retry. Persistent issue → tighten few-shot examples.
3. **Out-of-vocabulary `BlockType`.** Pydantic rejects; retry once. Almost never recurs after the retry preamble.
4. **Cost overrun on novel forms.** First-time forms route lots of blocks. Mitigation: per-document cost cap with alert (issue 10).
5. **Quote-inside-quote in branching expressions** (`Display if Q7 = "Lives in other's home"` — the `'` inside the value). DSL grammar must accept escaped quotes; LLM must emit them. Few-shot should include this case.
6. **Region/availability.** Bedrock model availability varies by region. Pin to a region where all three ladder models are available. v1 assumes `us-east-1`; verify before deploy.

## Open questions for v1 implementer

1. **PII in prompts.** Assessment text contains client names, SSNs (placeholders in templates, real values in production). Bedrock data-handling policies vary by model and region. Confirm the chosen model+region honors the no-training data-handling commitment (per AWS Bedrock data protection docs at deploy time). See issue 11.
2. **Batch size per Bedrock call.** Single-block calls are simplest but waste system-prompt tokens. Multi-block calls amortize but complicate validation. Recommend single-block for v1; revisit if Eval E shows the overhead matters.
3. **Few-shot bank growth strategy.** Each approved fixture becomes a candidate example. Cap the bank at N=20 and rotate; or grow unbounded and lean on retrieval (option 2/3). Recommend cap+manual curation for v1.
4. **What if the LLM correctly disagrees with the rule engine on a high-confidence block we never routed?** v1 has no path to surface this. Acceptable for v1; a future "shadow mode" could route a sample of high-confidence blocks for audit.

## Acceptance criteria

- ≥95% of LLM responses pass Pydantic validation on the first try.
- ≥99% pass after one retry.
- Of routed blocks that the LLM *re-classifies* (i.e. it disagrees with stage 2), Eval A type-accuracy on those blocks ≥ 0.85.
- Branching Logic syntactic validity rate ≥ 0.97 (Eval B `branching_syntactic_validity`).
- Per-PDF Bedrock spend ≤ $0.10 on the seed fixtures (Eval E).
- Determinism: two consecutive runs of `FX-CHOICES-001` against the prompt cache produce byte-identical refinements.

## Out of scope

- Routing decisions (issue 03).
- Sequence number assignment (issue 06).
- Post-processing the workbook (issue 07).

## Cross-references

- Upstream: `03-ambiguity-decision-gate.md`
- Downstream: `05-merge-enriched-json.md`, `06-golden-template-mapping.md` (sequence substitution into branching)
- Cost: `10-cost-and-latency-budget.md`
- Security: `11-security-pii-handling.md`
- Determinism / caching: `09-observability-and-logging.md`
- Eval that scores LLM behaviour: `evals/eval-A-enriched-json.md` (`branching_logic_exact_match`), `evals/eval-D-semantic-equivalence.md` (catches semantically-equivalent variants)
