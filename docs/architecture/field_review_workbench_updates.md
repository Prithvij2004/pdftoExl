# Field Review Workbench Updates

This branch adds a human review layer on top of the SHOWLAY PDF-to-Excel flow. The main assumption is simple: the agent can make mistakes, so reviewers need to see the extracted row, the PDF evidence, and the editable output in one place.

## What Changed

The app now creates a field-level review manifest after extraction. That manifest keeps the workbook row data, confidence signals, review reasons, page information, and bounding box evidence together. The workbench reads this manifest and shows the PDF beside the extracted rows, so a reviewer can check what the agent saw.

The review screen is now meant for non-technical users. The left side is a row list, the center is the PDF page, and the right side is an editable form called Review Output instead of raw JSON. The raw JSON is still available, but it is hidden behind an Advanced toggle.

## Review Behavior

Hovering a row highlights the matching box on the PDF. Hovering a box on the PDF selects the matching row in the sidebar. This works without clicking, so reviewers can scan quickly.

Page navigation works through buttons and scrolling. When the active row is on a different page, the PDF view can move to that page and show the correct evidence.

Reviewers can edit field values directly, remove rows, or add a new row after any existing row. The app resequences rows before saving, so inserted rows stay in the right order.

## Bounding Box Fixes

The earlier bbox behavior could point to the wrong repeated text because the same label can appear in multiple places. The update makes evidence matching more order-aware and splits inline text spans more carefully, so repeated labels like dates and signatures can map to the matching row instead of jumping to another copy on the page.

This does not mean bbox matching is perfect. It means the review UI now treats bbox evidence as review help, not as final truth. The reviewer can still fix the row if the agent or matcher is wrong.

## Repeated Field Fixes

Repeated fields are no longer dropped just because their text is the same. The post-processing step now keeps repeated table or fall-instance rows when they mean different things in context.

For example, a Date field repeated across multiple fall entries should appear multiple times in the output. Repetition is removed only when it is the same field repeated as the same meaning, such as a running header or duplicate chrome.

## Save Review Output

Save Review now does more than save the manifest. It rebuilds both Excel outputs from the reviewed rows:

- `runtime/output_web/<job_id>.xlsx`
- `runtime/output_web/<job_id>_review.xlsx`

The API returns `saved_and_outputs_updated`, and the UI shows `Saved and output updated`. This means the reviewed changes are carried into the downloadable workbook files.

It does not regenerate the source PDF. The PDF is the input evidence. The final generated artifact is still the Excel workbook.

## Files Touched

The main implementation is in:

- `SHOWLAY-APPROACH/webapp.py`
- `SHOWLAY-APPROACH/showlay/field_review.py`
- `SHOWLAY-APPROACH/showlay/postprocess.py`
- `SHOWLAY-APPROACH/showlay/schema.py`

The main tests are in:

- `tests/unit/test_showlay_field_review.py`
- `tests/unit/test_showlay_webapp_manifest_save.py`
- `tests/unit/test_showlay_postprocess.py`

## Verification

The branch has been checked with:

```bash
SHOWLAY-APPROACH/.venv/bin/python -m py_compile SHOWLAY-APPROACH/webapp.py
python3 -m ruff check SHOWLAY-APPROACH/webapp.py tests/unit/test_showlay_webapp_manifest_save.py
python3 -m pytest -q
python3 -m build --wheel
```

The current sample job `7523f6788391` was also saved through the live API, and the response confirmed that both workbook files were updated.
