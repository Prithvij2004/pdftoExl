# Agentic Extractor — Strengths & Problems

Scope: `app/services/agentic_extractor.py`, `app/services/pdf_structure.py`,
`app/services/normalize.py`, `app/services/extractor.py`, `app/models.py`,
`app/services/pdf_batches.py`.

## What's good

1. **Structured output via Pydantic AI.** `BatchAnalysis` / `FieldCandidate`
   (`agentic_extractor.py:21-62`) gives schema-validated output instead of
   regex'd JSON. A real upgrade over the legacy `extractor.py` path which
   still does `_parse_model_json` on raw text.

2. **Native AcroForm structure as a grounding signal.** `pdf_structure.py`
   reads `/FT`, `/Ff`, `/Opt`, `/AP`, etc. and turns them into
   `pdf_input_type` + `question_type_hint`
   (`pdf_structure.py:171-190`). Feeding this into the prompt is a real
   improvement — the model no longer has to guess "blank box → Text Box".

3. **Single-page batching is the right call.** `_renumber_rows`
   (`agentic_extractor.py:186-197`) authoritatively sets `page_number` /
   `source_order` from the batch boundary instead of trusting the model.
   The comment there is honest about why.

4. **Header detection is layout-aware.** Top-fraction y-coordinate cutoff
   with min/max clamp (`pdf_structure.py:323-326`), plus pymupdf-then-pypdf
   fallback. The "repeated across pages" check
   (`pdf_structure.py:374-380`) is a clean way to flag boilerplate.

5. **Deterministic post-processing.** `normalize.py` does the heavy lifting
   that LLMs do badly: continuation merging with explicit-marker gates,
   choice-type re-tagging from wording, header/footer noise filtering.
   Keeping this out of the prompt is correct.

6. **Bold preservation via `**...**`.** Cheap, high-value for downstream
   Excel rendering.

7. **Specific Nova 424 detection.** `_is_truncated_tooluse_error` exists —
   though see issue #1 below: it's defined but never used.

## Problems

### 1. `_is_truncated_tooluse_error` is dead code

Defined at `agentic_extractor.py:176-183`, never called. `_process_batch`
catches `ClientError` / `BotoCoreError` but doesn't branch on truncation
to retry-with-smaller-context or split. Either wire it in or delete it.

### 2. Retry semantics are contradictory

`retries=2` at the Agent level combined with `retries={"max_attempts": 1}`
on the boto config: pydantic-ai retries on validation failure, boto retries
on transport. A 6000-token cap with a strict schema is exactly where
you'd hit truncation, and there is no fallback path.

### 3. Sequential page processing

The `for batch ... for sub` loop (`agentic_extractor.py:230-233`) is
`await`-ed serially. For an N-page PDF you pay N round-trips end-to-end.
Pydantic-AI agents are independent per page — `asyncio.gather` with a
semaphore would cut wall time dramatically.

### 4. `max_tokens=6000` is a silent cliff

Dense pages (long option lists, instruction-heavy forms) will truncate.
Combined with the strict `BatchAnalysis` schema, truncation =
validation error = lost page. No per-page fallback to a slimmer schema
or a "redo with text-only" path.

### 5. Prompt is enormous and duplicates `normalize.py` logic

The radio-vs-checkbox-vs-checkbox-group rules in `_analysis_prompt`
(`agentic_extractor.py:112-114`) are also enforced deterministically by
`_apply_choice_type_rules`. You're paying tokens for rules you re-apply
anyway. Trim the prompt to what only the model can do (vision, semantic
labeling) and let normalize own the type disambiguation.

### 6. Structural context is JSON-stringified into the prompt

`page_structure_prompt_context` dumps with `separators=(",", ":")` (good
for tokens) but the model has to parse JSON inside a vision prompt. A
short structured table (`native_field_id | type | label | options`)
would be cheaper and easier for the model.

### 7. `_MAX_PROMPT_FIELDS = 40` silently drops fields on dense pages

`pdf_structure.py:418` slices `page.fields[:40]` with no warning. Tax
forms easily exceed this. Either page-rank by position or chunk.

### 8. No confidence calibration / no abstention

`confidence` is Optional and the model fills it freely. Nothing
downstream uses it — not for filtering, not for review flagging. Either
drop it or actually use it (e.g., flag rows < 0.6 for human review).

### 9. Structure extraction is all-or-nothing

`structure` is computed once for the whole PDF but `extract_pdf_structure`
failures fall back to `None` for every page
(`agentic_extractor.py:222-225`). A single bad page (e.g. a malformed
annotation) kills structural grounding for the whole document. Make
structure extraction per-page-resilient.

### 10. No caching / no idempotency

Same PDF re-extracted = full re-spend. Hash the page bytes + prompt and
cache results to disk.

### 11. Header detection uses fixed coordinate fractions

`_HEADER_FRACTION = 0.15` is a guess. Forms with tall logos or banner
instructions break this. Consider: cluster top-y of repeated text across
pages instead of a hardcoded band.

### 12. Radio appearance state names leak as "options"

`_appearance_options` and `_options` may double-count or miss inherited
options on radio kids. The inheritance walk via `/Parent` is good, but
radio button kids' `/AP/N` keys often expose state names like
`Yes` / `Off` / `1` rather than human labels — feeding those as
"options" to the model is misleading. Filter or label them as raw
appearance states.

### 13. `QuestionType.DATE = "Calendar"` enum alias

`models.py:16`. Two enum members with the same value is a footgun —
`QuestionType("Calendar")` is fine but `QuestionType.DATE is
QuestionType.CALENDAR` may not behave as expected across
pickle/serialization. Remove the alias and migrate old strings via the
existing `_normalize_legacy_question_type` validator.

### 14. No eval feedback loop into the prompt

You have `evals/` and golden workbooks, but the prompt is hand-tuned.
Worth adding a small "common mistakes" few-shot block populated from
eval diffs.

### 15. Translation requirement is buried

"Translate non-English to English" is one line in the system prompt with
no examples. For multilingual forms this is the single biggest accuracy
lever and deserves explicit handling (or a separate translation pass).

## Suggested priority

1. Parallelize page calls with a semaphore (biggest perceived-quality
   win: speed).
2. Wire `_is_truncated_tooluse_error` into a retry-with-smaller-prompt
   path, or raise `max_tokens` and add a fallback.
3. Trim the prompt — move type-disambiguation rules out, lean on
   normalize.
4. Per-page resilient structure extraction; remove the 40-field cap or
   rank by position.
5. Add a disk cache keyed on page hash.
6. Decide on `confidence` — use it or drop it.
7. Remove the `DATE` enum alias.
