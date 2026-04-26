# PDF → Excel (FastAPI + Bedrock Nova)

Upload a PDF form and download a cleaned Excel file with these columns:

- `Question Type`
- `English Question/Index Text`
- `English Answer Text`

The extraction uses **Amazon Bedrock** with **Amazon Nova** (default: Nova Pro).

## Requirements

- Python 3.10+
- AWS credentials configured locally (for Bedrock)

## Setup

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Run

```bash
uvicorn app.main:app --reload
```

Open the app at `http://127.0.0.1:8000`.

## Configuration (environment variables)

- `AWS_REGION` (default: `us-east-1`)
- `BEDROCK_MODEL_ID` (default: `us.amazon.nova-pro-v1:0`)
- `PDF_BATCH_SIZE` (default: `4`)
- `MAX_UPLOAD_MB` (default: `25`)
- `DEBUG_JSON` (default: `0`)

## Evals (quality + performance)

This repo includes a small eval suite under `evals/` that compares the API output workbook to the expected golden `.xlsx` files in `docs/`.

Notes:
- `docs/` is listed in `.gitignore`, so evals will **skip** if you don't have the local fixtures.
- The end-to-end eval calls `POST /extract`, which invokes Bedrock and may be slow/costly; it is **opt-in**.

Run offline fixture/scoring checks (no AWS calls):

```bash
pytest evals
```

Run end-to-end API evals (uploads `docs/*.pdf` to `/extract` and compares returned `.xlsx` to golden workbooks):

```bash
RUN_BEDROCK_EVAL=1 pytest evals -m bedrock -s
```

## Notes

- Generated Excel files are stored temporarily on local disk under `runtime/generated/` and served via a download endpoint.
- Uploads are stored under `runtime/uploads/` during processing.
