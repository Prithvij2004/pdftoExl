# Issue 08 — The golden template `.xlsx` as schema contract

## Summary

The golden template is not a blank spreadsheet — it is the **schema contract** for the workbook output. Column names, column order, the controlled vocabulary for `QuestionType`, the `Yes`/`No` dropdowns, formatting, frozen panes, the supporting sheets (`Description`, `Values`, `QA`, `Change Log`), and the metadata header block (rows 3–12) are all defined in the template. The pipeline writes *into* the template; it never recreates the schema in code. This issue documents the discipline that keeps that contract trustworthy.

A schema change is a template change, reviewed via PR diff, with `template_version` bumped. A code change that "fixes" a column name disagreement is a sign someone bypassed the contract.

## What the template encodes

From inspection of `docs/support_docs/CHOICES Safety Determination Request Form Final_11_20.xlsx`:

- **Sheets:** `Assessment v2` (the question rows), `Sheet1` (free-text reference notes), `Description` (field definitions), `Assessment` (older version of the question sheet — kept for back-compat), `Values` (controlled vocabularies), `QA` (reviewer discussion log).
- **Header block** in rows 3–12 of `Assessment v2`: `Assessment Title`, `Questionnaire ID`, `Program`, `Assessment Level Rule(s)`, `Question Level Rule(s)`, `Questionnaire Concept Code`, `Config Review Date`, `Config Approval Date`, plus contact-name fields.
- **Column header row** at row 13 (28 columns; full list in issue 06).
- **Data starts** at row 14.
- **Controlled vocab** in `Values` sheet:
  - Yes/No: `Yes`, `No`
  - QuestionType: `Radio Button`, `Checkbox`, `Checkbox Group`, `Text Area`, `Text Box`, `Date`, `Display`, … (read full list at implementation time)
  - Populated: `Admissions`, `Advance Directives`, `Allergies`, `Any with Concept Code (PP)`, `Care Team`, … (drives `Auto Populate with:` column)
  - Alert Type: `Info`, `Warning`, `High`
  - Display Text: `Bold`, `Underline `, `Italics`, `Red `, `Black` (note trailing spaces; preserved verbatim)
- **Data validation dropdowns** on the Yes/No, QuestionType, and Populated columns, sourced from the `Values` sheet via named ranges.

The template's **typos and trailing spaces are part of the contract** until intentionally fixed via a versioned template change:

- `"Auto Poulate Rule:"` (missing 'p' in "Populate") — col 22.
- `"IT Notes "` (trailing space) — col 28.
- `"Pre Populate\n(Yes / No)  "` (trailing spaces, embedded newline) — col 6.
- `"Underline "` and `"Red "` (trailing spaces) in `Values` sheet.

The pipeline writes by column index, not by header string match, so it tolerates these. But Eval B's per-column accuracy compares to the same template, so a "fix" silently regresses the eval.

## Versioning strategy

`template_version` is a **content hash + semver tag** of the template file:

- `template_version: "1.0.0+sha:<first-12-of-sha256>"`
- Major bump on column add/remove/reorder.
- Minor bump on controlled-vocab change (adding a `QuestionType`).
- Patch bump on formatting-only changes (column widths, colors).
- Hash bump on every byte-level change (catches accidental edits in Excel).

The pipeline reads `template_version` at startup, asserts it matches the version pinned in code, fails loud on mismatch. CI runs both seed fixtures against the pinned template; a template change that breaks an eval is rejected at PR review.

The template lives at `docs/support_docs/CHOICES Safety Determination Request Form Final_11_20.xlsx` for v1; production may copy it elsewhere but the source-of-truth is the repo path.

## How to evolve the template safely

1. Open the template in Excel or LibreOffice. Make the change.
2. Bump `template_version` per the rules above.
3. Regenerate Eval A and Eval B goldens for every fixture (script: `make eval-update-goldens`).
4. Diff the goldens: only the affected cells should change. If unrelated cells differ, the template change had unintended consequences — investigate.
5. PR with the template diff (use a tool that diffs `.xlsx` cell-by-cell — `xlsx-diff` or a custom script), the version bump, and the regenerated goldens.

## Why we don't programmatically generate the template

Considered and rejected:

- **Code defines the schema, generate the template at build time:** removes the "open-and-append" advantage, recreates every formatting rule in code, and forces every change through code review even when it's a one-cell label tweak. Net negative.
- **Two artifacts: a YAML schema + a code-generated template:** doubles the contract surface; the YAML and Excel will drift.
- **Template inferred from the reference workbook on first run:** loses the "review the template change in PR" property.

The .xlsx-as-schema discipline trades programmer comfort for trust. Keep it.

## Known failure modes

1. **Excel "helpfully" reformats cells on save.** A reviewer opens the template, makes no intended changes, but Excel rewrites date formats or strips trailing spaces. Mitigation: PR diff catches it; reject and re-edit with care (or use LibreOffice in batch mode).
2. **Named-range corruption** when sheets are renamed. Don't rename sheets without also updating named ranges and bumping the major version.
3. **Hidden rows / hidden sheets** introduced by accident: dropdowns may stop working. Verify visually after every template change.
4. **Schema change that adds a `QuestionType` value the rule engine doesn't know about.** Evals will pass but production output may regress. Mitigation: every controlled-vocab addition is accompanied by a stage 2 / stage 6 mapping update in the same PR.
5. **Per-state template variants.** Different states may want column tweaks. v1 supports only a single template; multi-template support is v2 territory.

## Open questions for v1 implementer

1. Should `template_version` be embedded in a hidden cell of the template itself (so the file is self-describing) or only tracked in code? Recommend both: cell `A1` of a hidden `_meta` sheet AND a constant in code.
2. Diff tooling: is there a maintained `xlsx-diff` we like, or do we write a small one in `pandas`/`openpyxl`? Recommend the latter — 50 lines, no external dep.
3. Do we want a "lint the template" CI job that checks invariants (header row at row 13, 28 columns, named ranges intact)? Strong yes.

## Acceptance criteria

- The pipeline refuses to run if `template_version` mismatch.
- A template lint job runs in CI and asserts the structural invariants on every PR.
- Evals A and B have a documented golden-update procedure tied to `template_version` bumps.

## Out of scope

- Writing into the template (issue 07).
- Mapping logic (issue 06).
- v2's potential multi-template support.

## Cross-references

- Used by: `06-golden-template-mapping.md`, `07-excel-writer-output.md`
- Eval that pins to template: `evals/eval-B-workbook.md`
