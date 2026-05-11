# PDF to Excel POC

## Full PDF to Excel Pipeline

The current pipeline is staged and audit-friendly:

1. PDF to Markdown using the selected parser.
2. Markdown semantic chunking.
3. LLM hierarchical extraction.
4. LLM refinement.
5. Validation and review flags.
6. Workbook row mapping.
7. Deterministic Excel generation.

The frontend template remains unchanged. Uploading a PDF through the existing form now returns JSON with a Markdown preview, intermediate artifact paths, and an Excel download URL.

## Environment

```env
PDF_PARSER=docling
OUTPUT_DIR=outputs
DOWNLOADS_DIR=C:\Users\299778\Downloads

AWS_ACCESS_KEY_ID=
AWS_SECRET_ACCESS_KEY=
AWS_REGION=
BEDROCK_MODEL_ID=

LLM_PROVIDER=bedrock
LLM_TEMPERATURE=0
LLM_MAX_TOKENS=8192
```

Parsers can be switched through `.env`:

- `docling`
- `pdfplumber`
- `pymupdf`

Docling may require separate installation:

```powershell
.\venv\Scripts\python.exe -m pip install docling
```

## Run App

```powershell
.\venv\Scripts\python.exe -m pip install -r requirements.txt
.\venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8010
```

Open:

```text
http://127.0.0.1:8010
```

## CLI

PDF to Excel:

```powershell
.\venv\Scripts\python.exe scripts\run_pdf_to_excel.py --input samples\choices.pdf --parser docling
```

Markdown to Excel:

```powershell
.\venv\Scripts\python.exe scripts\run_markdown_to_excel.py --markdown outputs\markdowns\input.md
```

PDF to Markdown only:

```powershell
.\venv\Scripts\python.exe scripts\run_pdf_to_markdown.py --input samples\choices.pdf --parser pdfplumber --preview
```

## Outputs

Intermediate audit files are saved under `OUTPUT_DIR`:

- `markdowns/`
- `chunks/`
- `extraction/`
- `validation/`
- `workbook_rows/`
- `review/`

The final Excel workbook is saved under `DOWNLOADS_DIR`, using the uploaded PDF name:

```text
C:\Users\299778\Downloads\<pdf_name>_generated.xlsx
```

Review the validation issues and review report before using the generated workbook as final.

## Notes

- The LLM must return schema-valid JSON and include source text/source pages.
- Uncertain items are marked `needs_review=true`.
- Excel generation does not use the LLM; it uses validated structured JSON only.
- The system is designed to be auditable and reviewable, not perfect.
