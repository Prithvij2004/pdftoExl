# Hybrid PDF → Excel Workflow

*Step-by-step detail of each stage, the tools involved, and where deterministic logic hands off to the LLM.*

---

This workflow converts state-approved assessment PDFs into EAB-ready Excel workbooks. Docling handles deterministic structure extraction, rule-based classification assigns semantic types where confidence is high, and an LLM on Amazon Bedrock refines only the ambiguous cases. The final enriched JSON is mapped against a golden template to produce a workbook ready for business review.

Each step below includes a short description of what happens, why it matters, and the specific libraries or services used.

---

## Step 01 · Extraction — Canonical JSON (raw)

Docling converts the input PDF into a structured document tree. Each page is decomposed into blocks: paragraphs, headings, lists, table cells, and form elements. Output is a lossless JSON representation with bounding boxes, reading order, and parent-child containment preserved.

No semantic interpretation yet. The goal is a faithful digital twin of the PDF that downstream stages can reason about without re-parsing the raw file.

**Tools & libraries**
- **Docling** (IBM, MIT-licensed) — primary parser
- **Granite-Docling-258M** — VLM for complex layouts
- **TableFormer** — table structure recognition
- **docling-serve** — FastAPI wrapper for deployment
- **DocTags** — markup format for faithful structure preservation

---

## Step 02 · Deterministic — Rule-based classification

Each block from stage 1 is passed through a rule engine that assigns a semantic type: section header, question label, checkbox option, text input, table cell, display-only instruction, signature field, and so on. Rules look at structural cues (font weight, indentation, trailing colon, nearby underscores or checkbox glyphs, position within the page).

Every classification carries a confidence score. Blocks that match strong rules get high confidence and bypass the LLM entirely in later stages.

**Tools & libraries**
- **Python rule engine** — custom, repo-local
- **Pydantic** — block schema validation
- **regex** — pattern libraries
- **spaCy** (optional) — lightweight linguistic features

---

## Step 03 · Decision gate — Ambiguity detection

Blocks are routed based on classification confidence and structural clarity. Three conditions trigger refinement:

- Confidence below 0.70
- Unclear parent-child relationships (e.g., a trailing text blank with no obvious parent checkbox)
- Blocks where multiple semantic types are plausible

High-confidence blocks flow straight to the merge stage. Only the genuinely ambiguous subset is sent to the LLM, which keeps cost and latency bounded.

**Tools & libraries**
- **Threshold logic** — configurable per block type
- **Graph analysis** — orphan-field detection
- **Structured logging** — downstream eval debugging

---

## Step 04 · LLM refinement — Intelligent correction on ambiguous blocks

The ambiguous blocks are sent to an LLM on Amazon Bedrock with a compact prompt: the block itself, its neighbours for context, the golden template's controlled vocabulary, and a small set of few-shot examples pulled from previously approved workbooks.

The model returns a corrected type, a parent-block reference if applicable, and a branching-logic expression in the syntax the template expects (for example, `Display if Q7 = Lives in other's home`). Structured outputs guarantee schema-valid JSON with no post-processing.

**Tools & libraries**
- **Amazon Bedrock Converse API** — with structured outputs
- **Amazon Nova Pro** — primary model, lowest cost on Bedrock
- **Mistral Large 2** or **Llama 3.3 70B** — fallback for harder cases
- **Pydantic** — JSON schema input and output validation

---

## Step 05 · Merge — Final enriched JSON

High-confidence rule-based outputs and LLM-refined outputs are merged into a single canonical structure. Every block now has a type, a confidence score, optional parent references, and branching logic where applicable.

The merged result is validated against a strict Pydantic schema. Invalid entries (e.g., a `QuestionType` value outside the controlled vocabulary) fail loudly here rather than silently producing bad Excel rows later.

**Tools & libraries**
- **Pydantic** — schema validation
- **Python dataclasses** — merged model representation
- **jsonschema** — external schema export

---

## Step 06 · Mapping — Workbook mapping against the golden template

Each enriched block becomes one row in the Excel workbook. The golden template defines the 26-column schema: `Section`, `Sequence`, `QuestionType`, `Question Text`, `Branching Logic`, `Answer Validation`, and so on.

Columns fall into three categories:

- **Extractable** — filled from the enriched JSON (Question Text, QuestionType, Branching Logic, etc.)
- **Default** — set to template defaults (Alert, Auto Populated, History, Pre Populate, Submission History → usually "No")
- **Preserved blank** — left empty for human reviewers (Concept Code, Token ID, IT Notes, Questionnaire ID)

**Tools & libraries**
- **openpyxl** — cell-level writes
- **Pydantic** — row models with `Literal` types on constrained columns
- **Golden template .xlsx** — versioned in the repo as the schema contract

---

## Step 07 · Output — EAB-ready Excel workbook

The workbook writer opens the golden template as a base file and appends the mapped rows. This preserves cell formatting, data validation dropdowns, frozen panes, and the supporting sheets (Description, Values, QA, Change Log) without needing to recreate them.

The final output is structurally ready for direct import into the EAB configuration tool. Branching logic is validated syntactically, sequence numbers are contiguous, and controlled vocabularies are enforced. The file then moves to business review (Step 3 of the parent Configuration Workflow).

**Tools & libraries**
- **openpyxl** — format-preserving writes
- **AWS S3** — storage and reviewer handoff
- **Amazon SNS** or **SES** — reviewer notification

---

## Golden template — the output contract

The golden template is a versioned `.xlsx` file that defines the exact output schema. It is not a blank spreadsheet. It encodes:

- The 26-column header row with exact column names and order
- Data validation dropdowns on constrained columns (`QuestionType`, Yes/No fields)
- Cell formatting, column widths, frozen panes, tab colors
- Supporting sheets: *Description* (field definitions), *Values* (controlled vocabularies), *QA*, *Change Log*
- The metadata header block (Assessment Title, Program, Contact Name, approval dates)

Because the writer opens this file as the base for every output, any schema change is made once in the template and propagates automatically. The template is version-controlled alongside the code so changes are reviewable.

---

## Local eval checkpoints

Two checkpoints catch regressions early and localize bugs.

### Eval A · post-stage 5 — Structure check on enriched JSON

Compares the enriched JSON against a hand-curated expected JSON for each fixture.

**Metrics:** type accuracy per block, parent-link F1, branching-logic exact match, sequence correctness.

Runs the rule-only path without Bedrock to keep CI cheap; full pipeline runs nightly.

**Tools & libraries**
- **pytest** — test harness
- **deepdiff** — structured diffs
- **scikit-learn** — F1 scores on parent-link detection

### Eval B · post-stage 7 — Workbook check on generated Excel

Compares the generated `.xlsx` against the reference `.xlsx` cell by cell.

**Metrics:** row-count match, per-column accuracy, schema validity (`QuestionType` values inside controlled vocabulary, Yes/No columns valid), branching-logic syntactic match.

Catches mapping and serialization errors that would slip past a JSON-only eval.

**Tools & libraries**
- **openpyxl** — cell-by-cell comparison
- **pandas** — tabular diff reports
- **pytest** — assertions with configurable thresholds

---

*Failures in either eval feed back into prompt refinement (for LLM errors) or rule engine updates (for deterministic errors). Each approved output becomes a new fixture, so eval coverage grows with production use.*
