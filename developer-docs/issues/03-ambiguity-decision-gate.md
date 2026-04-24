# Issue 03 — Stage 3: Ambiguity decision gate

## Summary

The gate is the cheapest stage in the pipeline and the most important one for cost. It looks at every classified block from stage 2 and decides: **does this block need the LLM, or is the rule-based answer good enough?** Get the gate too tight and you pay LLM cost on blocks the rule engine already nailed. Get it too loose and bad classifications flow into the workbook.

## Inputs / Outputs

**Input:** `ClassifiedDocument` from stage 2.

**Output:** `GatedDocument`:

```python
class GateDecision(BaseModel):
    block_id: str
    routed_to: Literal["pass_through", "llm_refine"]
    reason: List[GateReason]            # one or more triggers
    threshold_used: float

class GateReason(str, Enum):
    LowConfidence       = "low_confidence"        # confidence < threshold
    OrphanInput         = "orphan_input"          # TextInput with no parent_link
    ConflictingRules    = "conflicting_rules"    # top-2 rule scores within ε
    UnknownType         = "unknown_type"           # block_type == Unknown
    StructuralAnomaly   = "structural_anomaly"   # graph-detected: dangling parent ref, etc.
```

A passing block is forwarded to stage 5 (merge) untouched. A routed block is sent to stage 4 (LLM) along with its neighbours and parent context.

## v1 design decision

Three trigger conditions, any of which routes a block to the LLM:

1. **Low confidence:** `confidence < 0.70` (configurable per `BlockType`).
2. **Structural anomaly:** orphan inputs (`TextInput` / `TextArea` / `DateInput` with no `parent_link`), checkboxes with no nearby `QuestionLabel` or `SectionHeader`, dangling parent references.
3. **Plausible-multi-type:** the top-2 rule confidences are within ε=0.05 of each other.

Why 0.70 and not 0.5 or 0.9:
- At **0.5**, ~95% of CHOICES blocks pass through (most rule misfires happen above 0.5). LLM only sees genuine `Unknown`. Cost is minimal but quality regression risk: borderline 0.6 blocks that should have been corrected slip through.
- At **0.9**, ~40% of blocks go to the LLM. Quality is excellent but cost balloons (~$0.15–$0.25/PDF on Nova Pro). Latency triples.
- At **0.70**, an estimated 15–25% of blocks route to the LLM on the seed fixtures. This is the empirically tuned sweet spot for v1; revisit after first 50 production fixtures.

This is a v1 calibration. The bar to change it is "show me the cost-vs-Eval-A curve at the new threshold."

## Design space considered

| Option | Why not (for v1) |
|---|---|
| **No gate (everything to LLM)** | Defeats the hybrid design. ~10× cost, plus the LLM is *worse* than rules on the easy cases. |
| **No LLM (everything pass-through)** | Eval A type accuracy drops ~7pp on the seed fixtures based on rule-only pilot. Workbook becomes unreliable on novel forms. |
| **Single global threshold** | What v1 uses. Simple, debuggable. |
| **Per-`BlockType` threshold** | Marginally better (e.g. checkbox glyphs are unambiguous; relax their threshold). v1 makes thresholds *configurable* per type but defaults to a single 0.70. |
| **Learned gate (small classifier on confidence + features)** | Better in principle, but training data is limited and the gain over a tuned threshold is small. Defer to v2. |
| **Cost budget gate** ("send the lowest-confidence N% per document up to budget X") | Useful for production cost control but couples the gate to cost reasoning. Out of scope for v1; revisit when Eval E is feeding into a cost ceiling. |

## Orphan / structural-anomaly detection

Build the document as a directed graph from `parent_link` edges. Then:

- Any `TextInput` / `TextArea` / `DateInput` with no incoming or outgoing edges is an orphan input → route.
- Any `parent_link.parent_block_id` that doesn't resolve to an actual block (typo or dropped block) → route the child.
- Any cycle (shouldn't happen but cheap to check) → route every block in the cycle.
- Any `CheckboxOption` whose nearest preceding `QuestionLabel` or `SectionHeader` is more than 5 reading-order positions away → route (likely lost parent).

Implementation: `networkx.DiGraph` over block IDs; one pass per anomaly type. Sub-millisecond per fixture.

## Known failure modes

1. **Threshold drift between fixtures.** A rule that scores 0.71 on CHOICES might score 0.68 on a TX form because of a font-size difference. Mitigation: normalize confidence by document, OR widen the gate band (route the 0.65–0.75 range and let the LLM confirm). v1 does neither — it accepts the drift and relies on Eval A to catch it.
2. **Cascading false routes.** If stage 2 mis-classifies a parent as `Display` (low confidence), all its children become orphans and get routed too. One LLM call could fix the parent and incidentally rescue the children, but the gate routes them independently. Mitigation: stage 4 batches by parent-component.
3. **Routing the same block twice on retry.** The gate is deterministic; a retry should produce identical decisions. Verify with a fixed-seed run.
4. **Unknown-type avalanche on a novel form.** First time we see a new state's assessment, `Unknown` count may be 30%+. The gate will dutifully route them all. Open question below.

## Open questions for v1 implementer

1. **Should the gate cap routing at N% of blocks per document?** Bounds worst-case cost on novel forms but means some genuinely ambiguous blocks slip through. Recommend: no cap for v1; surface alert if route rate exceeds 30%.
2. **Should we batch routed blocks per LLM call?** Yes — see issue 04. The gate emits its decisions in batch-ready form (list of `(block, neighbours, doc_context_slice)` tuples).
3. **How does the gate report to observability?** Per-decision log line per issue 09. Aggregate metrics (route_rate, route_reasons histogram) per document.
4. **Where do per-`BlockType` overrides live?** Recommend: in a single `gate_thresholds.yaml` so they're version-controlled and reviewable separately from code.

## Acceptance criteria

- Route rate on rule-only stage 2 outputs is in `[0.10, 0.30]` for both seed fixtures.
- Every routed block carries ≥1 `GateReason`.
- Eval A type-accuracy improves by ≥3pp from rule-only to rule+LLM on the seed fixtures (i.e. the LLM is doing measurable work on what the gate sends it).
- Gate decisions are deterministic across reruns of the same `ClassifiedDocument`.

## Out of scope

- The LLM call itself (issue 04).
- Confidence calibration of rule-engine output (issue 02).
- Cost ceiling enforcement (revisit when issue 10's budget is implemented).

## Cross-references

- Upstream: `02-rule-based-classification.md`
- Downstream: `04-llm-refinement.md`, `05-merge-enriched-json.md`
- Cost lever: `10-cost-and-latency-budget.md`
- Logging: `09-observability-and-logging.md`
