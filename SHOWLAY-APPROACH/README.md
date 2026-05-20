# SHOWLAY-APPROACH - PDF to 28-column Assessment Excel

SHOWLAY converts assessment PDFs into the target 28-column Excel format, using
PDF structure, page images, a Bedrock-hosted VLM, post-processing, confidence
scoring, and review outputs.

## Setup

### 1. Create and activate a Python environment

From this folder:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
```

If PowerShell blocks activation, run this once in the same terminal:

```powershell
Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope Process
```

Then run the activation command again.

### 2. Configure environment variables

Copy the example file and fill in the real Bedrock credentials:

```powershell
Copy-Item .env.example .env
```

Required values:

```env
AWS_ACCESS_KEY_ID=...
AWS_SECRET_ACCESS_KEY=...
AWS_REGION=us-west-2
BEDROCK_VLM_MODEL_ID=qwen.qwen3-vl-235b-a22b
```

The app loads `.env` automatically from this folder.

### 3. Add the required XLSX template

The pipeline requires an `.xlsx` template workbook with the target 28-column
layout. It clones that workbook when writing the generated output, so the agent
cannot run from the CLI or web UI without a template file.

For CLI runs, the template can be anywhere as long as you pass its path as the
second argument to `run.py`.

For the web UI, place the template in one of these expected locations before
starting `webapp.py`:

```text
..\docs\support_docs\CHOICES Safety Determination Request Form Final_11_20.xlsx
..\SOURCE AND TARGET FILES\CHOICES Safety Determination Request Form Final_11_20 1.xlsx
```

## Run the Application

### Option A: Local web app

Before starting the web app, confirm the required `.xlsx` template exists in one
of the web UI template locations listed in setup step 3. The web app loads that
template at startup.

Start the web app:

```powershell
python webapp.py
```

Open:

```text
http://localhost:8000
```

Upload a PDF in the browser. The app shows extraction progress and provides
downloads for the generated workbook, review workbook, and review manifest.

Web outputs are written under:

```text
runtime\output_web\
```

### Option B: Single PDF from the command line

Run the end-to-end pipeline with a source PDF and a truth/template workbook. The
second argument is the required `.xlsx` template path:

```powershell
python run.py "..\SOURCE AND TARGET FILES\sph_rev25-3_H1700-3_final_approved 1.pdf" `
              "..\SOURCE AND TARGET FILES\TX LTSS - 1700-3, Individual Service Plan - Signature Page 1.xlsx"
```

Useful flags:

```powershell
python run.py <pdf_path> <truth_xlsx> --name <output_stem>
python run.py <pdf_path> <truth_xlsx> --no-eval
python run.py <pdf_path> <truth_xlsx> --dpi 200
```

CLI outputs are written under:

```text
runtime\output\<output_stem>\
eval_reports\<output_stem>\
```

### Option C: Batch run for the configured new PDFs

`run_new_pdfs.py` is preconfigured for the three PDFs listed in the script. It
uses the CHOICES workbook as the output template and skips evaluation.

```powershell
python run_new_pdfs.py
```

Before using it, confirm the expected source PDFs exist under:

```text
..\new pdf files\
```

## Output Files

For a normal CLI run, the output folder contains:

```text
<stem>.xlsx                 final 28-column workbook
<stem>_review.xlsx          companion human-review workbook
<stem>_review_manifest.json field-level review artifact with page evidence
<stem>_raw_vlm.json         raw per-page VLM JSON for debugging
<stem>_telemetry.json       per-page latency and token usage
```

If evaluation is enabled, `eval_reports\<stem>\` also contains:

```text
summary.json
summary.md
```

## Folder Structure

```text
SHOWLAY-APPROACH/
|-- .env                      local AWS and model configuration, gitignored
|-- .env.example              environment variable template
|-- README.md                 this file
|-- ARCHITECTURE_WORKFLOW.md  detailed architecture and workflow notes
|-- RESULTS.md                evaluation notes and results
|-- run.py                    end-to-end CLI runner
|-- run_new_pdfs.py           batch runner for the configured new PDFs
|-- webapp.py                 local FastAPI upload app
|-- requirements.txt          Python dependencies
|-- compare_agentic.py        comparison helper
|-- diff_truth.py             truth workbook diff helper
|-- replay_postprocess.py     post-processing replay helper
|-- showlay/
|   |-- schema.py             28-column Pydantic models, enums, and DSL
|   |-- extract.py            probe, rasterize, widgets, and VLM extraction
|   |-- postprocess.py        section, sequence, and branching post-passes
|   |-- confidence.py         per-row confidence aggregation
|   |-- field_review.py       field-level review manifest generation
|   |-- writer.py             template-based Excel writer
|   `-- eval.py               truth comparison and accuracy report
`-- runtime/
    |-- page_images/          generated 200 DPI page PNGs
    |-- extracted/            raw extraction artifacts
    |-- output/               CLI and batch output workbooks
    |-- uploads_web/          web-uploaded PDFs
    |-- output_web/           web app outputs
    `-- page_images_web/      web app page PNGs
```

## How It Works

The pipeline combines several evidence sources instead of relying on one PDF
parser:

```text
PDF
 |
 |-- probe          AcroForm, scanned/table-heavy signals, page count
 |-- rasterize      200 DPI PNG per page for VLM input
 |-- widgets        PyMuPDF AcroForm walk with bounding boxes and field types
 |-- text_layout    PyMuPDF coordinate-grounded text extraction
 |
 |-- VLM extract    Qwen3-VL on Bedrock, one call per page, 28-column JSON schema
 |
 |-- post-process   section forward-fill, sequence assignment, branching cleanup
 |
 |-- confidence     schema gate, span grounding, structural agreement
 |
 `-- write          final Excel workbook plus review sidecar
```

## Notes

- Primary VLM: `qwen.qwen3-vl-235b-a22b` on Bedrock in `us-west-2`.
- Qwen3-VL does not accept native PDF input here, so pages are rasterized before
  extraction.
- The optional verifier model is configured in `.env.example` but reserved for a
  later verifier pass.
- The target branching format is normalized toward REDCap-style expressions such
  as `If Q<n> = checked(selected)`.
