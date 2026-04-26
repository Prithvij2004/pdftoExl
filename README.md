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

## Notes

- Generated Excel files are stored temporarily on local disk under `runtime/generated/` and served via a download endpoint.
- Uploads are stored under `runtime/uploads/` during processing.
