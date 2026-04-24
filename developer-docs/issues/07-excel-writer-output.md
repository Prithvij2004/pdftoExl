# Issue 07 — Stage 7: Write the EAB-ready Excel workbook

## Summary

Stage 7 takes the `MappedWorkbook` (issue 06) and produces a `.xlsx` file that is structurally identical to a hand-crafted reference. The discipline is: **open the golden template, append rows, save**. Never construct the workbook from scratch. The template encodes formatting, data validation dropdowns, frozen panes, supporting sheets, and tab colors — all of which are tedious to reproduce in code and easy to drift on.

If this stage works correctly, the produced workbook drops directly into the EAB configuration tool (the parent Configuration Workflow's step 3) without any manual cleanup.

## Inputs / Outputs

**Input:** `MappedWorkbook` from stage 6 + `golden_template_path` (versioned `.xlsx`).

**Output:** a `.xlsx` file at the configured destination, plus a side-channel `WriteReport` (validation summary, used by Eval B).

```python
class WriteReport(BaseModel):
    output_path: str
    template_version: str
    rows_written: int
    sheets_present: List[str]                    # sanity check: Description, Values, QA, Change Log
    validations_preserved: bool                  # data-validation dropdowns still attached
    frozen_panes_preserved: bool
    warnings: List[str]
```

## v1 design decision

- **openpyxl** for cell-level writes. Open the template in `read_write` mode; never use `write_only` (which strips formatting).
- Open the golden template, locate the `Assessment v2` sheet (or `Assessment` for older fixtures — pin per `template_version`), and **append rows starting at the first empty row after the header row (row 13 for the CHOICES template)**.
- Header-meta block (rows 3–12) is updated in-place with `MappedWorkbook.header_meta`.
- Supporting sheets (`Description`, `Values`, `QA`, `Change Log`, `Sheet1`) are not modified.
- Save to a configured output path (local for dev, S3 for production).

## openpyxl pitfalls and how v1 dodges them

1. **Data validation extension warning.** openpyxl emits `"Data Validation extension is not supported and will be removed"` when reading the CHOICES template. The validations *do* survive a load+save cycle in practice, but verify with Eval B's `formatting_preservation` check on every release.
2. **Merged cells.** The header block uses merged cells. Writing into a merged range silently writes only the top-left cell. v1 writes header_meta to the top-left of each known-merged range and avoids touching anything else.
3. **Named ranges.** The template defines named ranges for the `Values` sheet (driving the dropdowns). Don't rename or move sheets, or the named ranges break.
4. **Conditional formatting.** Preserved through openpyxl in modern versions; verify with each library upgrade.
5. **Data types.** Write `int` for `Sequence` (not string); openpyxl does the right thing. For `Yes`/`No` columns, write the string `"Yes"`/`"No"` exactly as in the controlled vocab — case matters for the dropdown.
6. **Datetimes in header_meta** (config approval/review dates) should be `datetime.date` or `datetime.datetime` instances so Excel renders them as dates, not strings.
7. **Long strings** in `Question Text` (e.g. the multi-paragraph Display blocks on TX LTSS) must include literal `\n` for line breaks AND have row height auto-adjusted. openpyxl does not auto-adjust row height; v1 sets `ws.row_dimensions[r].height = None` and lets Excel compute on open. Verify visually.
8. **Encoding.** UTF-8 throughout. The CHOICES form contains curly quotes (`’`) and em-dashes (`—`); preserve verbatim.

## Why open-template-and-append, not build-from-scratch

| Approach | Cost | Reliability |
|---|---|---|
| Build from scratch (openpyxl `Workbook()`, recreate every sheet) | Hundreds of lines of formatting code; every template change needs a code change | Low; drift is constant |
| Use a templating library (e.g. `xlsxwriter` or `xltpl`) | Mid; learning curve; templates as separate files | Medium; one more dependency |
| Open the golden template and append (v1) | ~30 lines to write rows; template changes are template-only | High; the template *is* the contract |

This is also why v2 should not "improve" by switching to a programmatic template — the template-as-schema is the discipline that keeps the team honest.

## Output destinations

- **Dev:** local path, e.g. `out/{fixture_id}/{timestamp}.xlsx`.
- **Production:** S3 at `s3://<bucket>/eab-workbooks/{program}/{assessment_slug}/{timestamp}.xlsx`. Reviewer notification via SNS or SES (issue 11 covers PII implications). S3 lifecycle: 90-day retention by default; configurable per program.
- **CI:** ephemeral path, consumed immediately by Eval B.

## Known failure modes

1. **Template path drift.** The golden template lives in `docs/support_docs/` but production may need a copy elsewhere. Pin a single source-of-truth path; fail-fast if missing.
2. **Sheet rename.** If the template's question sheet is renamed (`Assessment` → `Assessment v2`), the writer must look up by `template_version` not by hard-coded sheet name.
3. **Append starting row.** v1 finds the first empty row after the header; if the template has stale example rows, those must be cleared in the template itself, not at write time (otherwise we conflate "remove old data" with "write new data").
4. **Concurrent writes.** Two pipeline runs writing to the same S3 path race. Mitigation: timestamp in path, or pre-acquire a lock object. v1 uses timestamp.
5. **Excel 32k-cell-character limit** on a single cell. Long display blocks may exceed it. Mitigation: hard fail with a clear error; surface as a candidate for splitting upstream.
6. **PII in the output filename.** `assessment_slug` should never be a member ID or name. Use program + assessment-title slug only.

## Open questions for v1 implementer

1. **Should we emit a side-by-side diff against the reference workbook in dev?** Helpful for manual review. Recommend yes; produce a `pandas.DataFrame.compare()` HTML diff alongside the .xlsx in dev mode.
2. **Should we write a `Change Log` row noting "auto-generated by pipeline v1.x.y on {date}"?** Useful audit trail. Recommend yes — append to existing `Change Log` sheet, don't replace.
3. **What about the `QA` sheet?** It contains discussion notes from human reviewers. The pipeline never writes to it; it's reviewer-only.

## Acceptance criteria

- Workbook opens in Excel without warnings (data validations intact).
- All non-`Assessment v2` sheets are byte-identical to the template (or differ only by appended `Change Log` rows).
- Frozen panes, column widths, tab colors, merged cells in the header block are preserved.
- Eval B passes its formatting checks on both seed fixtures.
- Per-fixture write latency < 5s on a typical workstation.

## Out of scope

- The mapping logic (issue 06).
- S3 lifecycle policy details (issue 11).
- The reviewer notification copy (operational, not architectural).

## Cross-references

- Upstream: `06-golden-template-mapping.md`
- Template contract: `08-golden-template-contract.md`
- Eval that scores this stage: `evals/eval-B-workbook.md` (formatting preservation, cell-by-cell)
- Security / S3: `11-security-pii-handling.md`
