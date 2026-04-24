# Issue 05 — Stage 5: Merge into the enriched canonical JSON

## Summary

Stage 5 fuses the rule-classified pass-through blocks with the LLM-refined blocks into a single, schema-validated structure. After this stage, every block has a final type, a final confidence, optional parent links, and optional branching logic. This is **Boundary 1** of the public contract (see `evals/contracts.md`); Eval A scores against it.

The stage is mostly bookkeeping, but two design points matter: precedence rules when rule and LLM disagree, and how invalid entries surface (loud failure now vs. silent bad workbook later).

## Inputs / Outputs

**Inputs:**
- `ClassifiedDocument` from stage 2 (every block, with rule-engine output).
- `List[LLMRefinement]` from stage 4 (subset of blocks the gate routed).

**Output:** `EnrichedDocument` — the public Boundary 1 artifact:

```python
class EnrichedBlock(BaseModel):
    block_id: str
    raw: RawBlock                                   # carried through unchanged from stage 1
    block_type: BlockType                           # final, after merge
    confidence: float                               # final, after merge
    parent_link: Optional[ParentLink]
    branching_logic: Optional[str]
    provenance: Provenance                          # rule_id and/or llm_model_id

    # populated by stage 6, but the schema reserves the slot here so the
    # Boundary-1 type is stable as the workbook contract evolves
    sequence: Optional[int] = None
    question_type: Optional[QuestionType] = None    # controlled vocab from golden template

class Provenance(BaseModel):
    source: Literal["rule", "llm", "merge"]
    rule_id: Optional[str]
    llm_model_id: Optional[str]
    overridden_from: Optional[BlockType]            # what this used to be before LLM override

class EnrichedDocument(BaseModel):
    source_doc: CanonicalDocument                   # full upstream artifact (for traceability)
    blocks: List[EnrichedBlock]
    schema_version: str                             # bump on any breaking change
```

The `EnrichedDocument` is what gets serialized to disk and what Eval A diffs against the golden.

## v1 design decision

**Precedence:** the LLM wins over the rule engine for routed blocks. The rule engine wins for everything else. No averaging, no voting. The rationale is that we *only* asked the LLM about blocks we knew the rule engine was uncertain about; on everything else it wasn't consulted.

**Validation:** strict Pydantic model with `Literal` types on enums. Invalid block_type, malformed branching syntax, out-of-vocabulary `question_type` (when populated by stage 6) → raise immediately. We do NOT silently coerce. Loud failure here is cheap; bad rows in a delivered workbook is expensive.

**No quarantine.** v1 does not have a "skip this block but continue" mode for invalid entries. If a block fails validation, the entire document fails. This sounds harsh; in practice it forces upstream stages to converge. The few-shot bank, gate thresholds, and rule library are the right places to fix problems — not a tolerant merge stage.

**Merge order is deterministic:** sort blocks by `(page, reading_order)`. Provenance is attached to every block so eval triage can answer "was this from the rule engine or the LLM?" without re-running the pipeline.

## Design space considered

| Option | Why not (for v1) |
|---|---|
| **Confidence-weighted vote between rule and LLM** | Both already passed a confidence bar by definition (rule ≥ 0.7, LLM ≥ 0.5). Voting between two confident sources yields no signal — and makes provenance ambiguous. |
| **Conservative merge** (when they disagree, mark `Unknown`) | Pushes problems to the operator. Defeats the purpose of LLM refinement. |
| **Quarantine invalid entries with `null` instead of failing** | Silent failure mode; invariably hides bugs. Rejected. |
| **Schema-by-version migration** (stage 6 reads stage 5 output through a translation layer) | Unnecessary indirection until we actually have a v2 schema. |

## Validation specifics

The Pydantic schema enforces:

- `block_type ∈ BlockType` enum.
- `0.05 ≤ confidence ≤ 0.99`.
- `parent_link.parent_block_id` exists in the document.
- `branching_logic`, if present, parses against the EAB DSL grammar (see issue 04).
- `provenance.source` is consistent with which inputs produced it (`rule` blocks have `rule_id`; `llm` blocks have `llm_model_id`).
- After stage 6 augments the document: `question_type ∈ QuestionType` enum (the controlled vocab from the golden template `Values` sheet).
- `sequence` (after stage 6): contiguous from 1, no gaps, no duplicates.

A `jsonschema` export of this schema is published to `developer-docs/evals/schema/enriched.schema.json` so v2 implementations can validate against it without depending on Python.

## Known failure modes

1. **Parent reference points to a block that was filtered out** (e.g. stage 1 dropped a duplicated page-header block). Validation catches this; surface with the both block IDs in the error message.
2. **LLM-emitted `branching_logic` references a `parent_link.parent_block_id` that itself has no `sequence`** (because the parent is `Display` or a header). Stage 6 will fail when it tries to substitute the sequence; better to catch at merge time. Validation rule: if `branching_logic` is set, `parent_link.parent_block_id` must be a block whose `block_type` has a sequence (one of the question/option/input types).
3. **Schema version drift.** When the schema changes, all existing fixtures' goldens become invalid. Mitigation: bump `schema_version`, regenerate goldens, version both in `evals/fixtures.md`.
4. **Provenance lost during merge.** Easy bug if the merge code doesn't carry `rule_id` through. Unit-test that every output block has a non-null provenance.

## Open questions for v1 implementer

1. Should `EnrichedDocument` embed the entire `source_doc`? It bloats serialized size 3–5×. Recommend yes for now (full traceability matters more than disk space at this scale); optional `--strip-source` flag for production output.
2. Where does the `jsonschema` export live? Recommend `developer-docs/evals/schema/enriched.schema.json`, regenerated by a `make schema` target.
3. Should we support partial merges (resume from stage 4 cache)? Useful for dev iteration, not required for correctness. Defer.

## Acceptance criteria

- Pydantic validation passes on both seed fixtures end-to-end.
- For every block in the output, `provenance.source` is set and consistent with which input it came from.
- The `jsonschema` export validates the same fixtures (parity check between the Python schema and the exported JSON Schema).
- Eval A passes its thresholds on `FX-CHOICES-001` and `FX-TXLTSS-001`.

## Out of scope

- Sequence number assignment (issue 06).
- `QuestionType` mapping (issue 06).
- Workbook writing (issue 07).

## Cross-references

- Upstream: `02-rule-based-classification.md`, `04-llm-refinement.md`
- Downstream: `06-golden-template-mapping.md`
- Boundary spec: `evals/contracts.md` (Boundary 1)
- Eval that scores this stage: `evals/eval-A-enriched-json.md`
