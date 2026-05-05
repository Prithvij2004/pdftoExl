# Requirements: PDF → Excel Form Extractor

## Problem

Government agencies publish intake, assessment, and eligibility forms as flat,
digitally-generated PDFs (not scans, can and cannot be AcroForm). Downstream systems need
these forms as structured data so they can be re-rendered in web/mobile
intake tools, diffed across revisions, and loaded into case-management
databases. Today this conversion is done by hand — slow, inconsistent, and
unable to keep up with form revisions across dozens of agencies (TennCare,
Texas HHSC, federal CMS, IRS, state agencies, etc.).

The system must convert any such PDF into a fixed-schema Excel workbook
without form-specific code paths.

## Output schema

A single-sheet `.xlsx` workbook with exactly these seven columns, in order:

```
Section | Sequence | Question Rule | Question Type | Question Text | Branching Logic | Answer Text
```

- Row 1 is a styled header (bold, dark-blue fill, frozen).
- `Sequence` is a dense 1..N integer in document order.
- `Question Type` is restricted to the canonical taxonomy below.
- `Branching Logic` uses only the two templates defined below.
- Data rows wrap text and top-align.

## Question Type taxonomy

Every row's `Question Type` must be one of:

```
Display, Text Box, Text Area, Date, Number, Signature,
Radio Button, Checkbox, Dropdown, Group Table
```

Composite "with Text Area" types are not allowed — they must be split into a
parent row plus a separate gated `Text Area` row.

- Display: These are blocks that are to used on last case and when the fields are just instruction or knoweldge for a user who fills the pdf.
- Text Box: These are fields where the input is one line length and supports for alphanumerics.
- Text Area: These are fields where the input is multi line and supports alphanumerics.
- Date: These are fields that support date input.
- Number: These are fields that only support Number input.
- Signature: These are the fields where signature needs to be used.
- Radio Button: These are the fields that asks a questions which has multually exclusive answer options and only one can be selected. Here, the Question Text needs to be the Question asked and the following answer options should be in the Answer Text part.
- Checkbox: These are fields that has looks like a checkbox with a box.
- Dropdown: These are fields has select only one answers from set of options.
- Group Table: These are the fields with the group table name as the Question Text. And The fields inside Table should be in the following fields. =

The branching logic field needs to be filled if the current field comes under the Radio Button, Chekbo and Dropdown. 

## Functional requirements

### Input handling
- Accept any digitally-generated PDF form, single- or multi-page.
- Do not require AcroForm widgets; layout-only PDFs must work.
- Scanned PDFs are out of scope.

### Form semantics that must be recovered
1. **Sections.** The `Section` column is not required and may be blank. When
   a section heading appears, the `Section` column is populated with that
   heading on every row from that point until the next section heading
   (rows before the first section have a blank `Section`). At the start of
   each new section, a single marker row is emitted with
   `Question Type: Display`, `Question Text: "New Section"`, and
   `Answer Text: <section name>`. This marker is emitted only at the
   section boundary, not on subsequent rows of the same section.
2. **Questions and fields.** Every prompt that expects an answer becomes a row
   with the correct `Question Type`. `Label: ____` blanks must be detected and
   typed by label semantics (`date|dob|birth` → Date; `score|count|age|number|#`
   → Number; lone `signature` → Signature; else Text Box).
3. **Options.** Consecutive options after a question form its answer set.
   Mutually-exclusive options are `Radio Button`; independent flags are
   `Checkbox Group`; a lone option is `Checkbox`. Square glyphs (`☐`) alone
   must not force `Checkbox Group` — explicit instruction wording
   (`select one`, `check all that apply`) and semantic exclusion take
   precedence over glyph shape.
4. **Tables.** Any multi-row table becomes one `Group Table` parent row plus
   one row per column header (typed by column-label inference). Data rows
   inside tables are not extracted.
5. **Checkbox follow-ups.** An indented `o`-bulleted prompt or
   `Description of documentation attached: ____` line under a checkbox is a
   sibling gated question (`Text Area` / `Text Box`), not an option of the
   checkbox.
6. **Specifiers.** A `Specify` / `Specify other` text field after an option
   containing `other`/`specify` becomes a separate row gated on that exact
   option text.
7. **Repeat bands.** Decorative repeats (page numbers, form codes, agency
   strings) are dropped on every page. Identifier banners are kept
   on the first page only and promoted to questions.
8. **Cross-page stitching.** A question whose options spill onto the next
   page must merge into a single logical row.
9. **Question Rule.** Leading `For <population>:` prefixes or italic prelude
   blocks must populate the `Question Rule` column.

### Branching logic
Only two templates are permitted in the `Branching Logic` column:
- `If Q{n} = checked(selected)` — for children gated by a checkbox.
- `Display if Q{n} = {option_text_verbatim}` — for children gated by a radio
  / dropdown option. Option text must be quoted exactly as it appears in the
  parent's answer set.

Free-text branching expressions are rejected. Indent-based branching is
inferred only within a single page; cross-page branching is not inferred.

### API
- `POST /extract` (multipart `file`): accepts a PDF, runs the pipeline,
  returns `{job_id, row_count, download_url}`.
- `GET /download/{job_id}`: streams the produced `.xlsx`.
- `GET /health`: liveness check.

### Quality bar
- ≥ 90% row-level match against each reference golden in `docs/reference/`
  (H1700-3 signature page; CHOICES Safety Determination).
- 100% of produced `Question Type` values fall within the 12-value taxonomy.
- `Sequence` is dense and gap-free.
- Every section in the output has a corresponding synthetic section marker row.
- Produced workbooks are visually identical in styling to the goldens so a
  diff tool can compare like-for-like.

### Generalization
The system must work on PDFs it has never seen, from agencies whose forms
are not in the reference set, without code changes. All heuristics are
structural (layout, glyphs, indent, blank-field patterns), never
form-specific.
