from __future__ import annotations

import os
import uuid
import json
from pathlib import Path

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError, NoCredentialsError
from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates

from app.config import settings
from app.services.excel_writer import write_rows_to_xlsx
from app.services.extractor import _ensure_inference_profile_id, extract_rows_from_pdf
from app.services.normalize import normalize_rows


app = FastAPI(title="PDF → Excel Extractor", version="0.1.0")
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

PDF_MIME_TYPES = {"application/pdf", "application/x-pdf"}


@app.on_event("startup")
def _startup() -> None:
    settings.runtime_dir.mkdir(parents=True, exist_ok=True)
    settings.uploads_dir.mkdir(parents=True, exist_ok=True)
    settings.generated_dir.mkdir(parents=True, exist_ok=True)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}

@app.get("/aws-test")
def aws_test():
    """
    Verifies:
    - AWS credentials resolve (STS GetCallerIdentity)
    - Bedrock Runtime is reachable and model is invokable (Converse)
    """
    try:
        sts = boto3.client("sts", region_name=settings.aws_region)
        ident = sts.get_caller_identity()
    except NoCredentialsError as e:
        raise HTTPException(
            status_code=401,
            detail="AWS credentials not found. Configure AWS_ACCESS_KEY_ID/AWS_SECRET_ACCESS_KEY (and optional AWS_SESSION_TOKEN).",
        ) from e
    except (ClientError, BotoCoreError) as e:
        raise HTTPException(status_code=502, detail=f"STS call failed: {e}") from e

    cfg = Config(connect_timeout=15, read_timeout=30, retries={"max_attempts": 1})
    brt = boto3.client("bedrock-runtime", region_name=settings.aws_region, config=cfg)

    try:
        effective_model_id = _ensure_inference_profile_id(settings.bedrock_model_id, settings.aws_region)
        resp = brt.converse(
            modelId=effective_model_id,
            messages=[{"role": "user", "content": [{"text": "Reply with exactly: OK"}]}],
            inferenceConfig={"maxTokens": 5, "temperature": 0.0, "topP": 0.1},
        )
        content = resp.get("output", {}).get("message", {}).get("content", [])
        text = (content[0].get("text") if content and isinstance(content[0], dict) else "") or ""
    except (ClientError, BotoCoreError) as e:
        raise HTTPException(
            status_code=502,
            detail=f"Bedrock converse failed (check region/model access/permissions): {e}",
        ) from e

    return {
        "ok": True,
        "aws_region": settings.aws_region,
        "bedrock_model_id": effective_model_id,
        "caller_identity": {
            "account": ident.get("Account"),
            "arn": ident.get("Arn"),
            "user_id": ident.get("UserId"),
        },
        "bedrock_response_preview": text.strip(),
    }


@app.get("/", response_class=HTMLResponse)
def index(request: Request) -> HTMLResponse:
    return templates.TemplateResponse("index.html", {"request": request})

def _safe_filename(name: str) -> str:
    base = os.path.basename(name or "upload.pdf")
    base = base.replace("\x00", "")
    if not base.lower().endswith(".pdf"):
        base += ".pdf"
    return base[:180]


async def _persist_upload_pdf(file: UploadFile) -> tuple[str, Path]:
    if file.content_type not in PDF_MIME_TYPES:
        raise HTTPException(status_code=400, detail="Only PDF uploads are supported.")

    file_id = uuid.uuid4().hex
    original_name = _safe_filename(file.filename or "upload.pdf")
    out_path = settings.uploads_dir / f"{file_id}__{original_name}"

    max_bytes = settings.max_upload_mb * 1024 * 1024
    total = 0

    with out_path.open("wb") as f:
        while True:
            chunk = await file.read(1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > max_bytes:
                try:
                    out_path.unlink(missing_ok=True)
                except Exception:
                    pass
                raise HTTPException(
                    status_code=413,
                    detail=f"File too large. Max upload is {settings.max_upload_mb} MB.",
                )
            f.write(chunk)

    # quick magic header check
    with out_path.open("rb") as f:
        if f.read(5) != b"%PDF-":
            try:
                out_path.unlink(missing_ok=True)
            except Exception:
                pass
            raise HTTPException(status_code=400, detail="Uploaded file is not a valid PDF.")

    return file_id, out_path


@app.get("/download/{file_id}")
def download(file_id: str):
    # For v1, file_id maps to a generated xlsx saved as: runtime/generated/{file_id}.xlsx
    xlsx_path = settings.generated_dir / f"{file_id}.xlsx"
    if not xlsx_path.exists():
        raise HTTPException(status_code=404, detail="File not found.")

    return FileResponse(
        path=str(xlsx_path),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        filename="extracted.xlsx",
    )


@app.post("/extract")
async def extract(file: UploadFile = File(...)):
    file_id, pdf_path = await _persist_upload_pdf(file)

    try:
        rows = extract_rows_from_pdf(pdf_path)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e

    rows = normalize_rows(rows)
    if not rows:
        raise HTTPException(status_code=422, detail="No extractable content found in the PDF.")

    xlsx_path = settings.generated_dir / f"{file_id}.xlsx"
    write_rows_to_xlsx(rows, xlsx_path)

    if settings.debug_json:
        debug_path = settings.generated_dir / f"{file_id}.json"
        debug_payload = [
            {
                "question_type": r.question_type.value,
                "question_text": r.question_text,
                "answer_text": r.answer_text,
                "page_number": r.page_number,
                "source_order": r.source_order,
                "confidence": r.confidence,
                "meta": r.meta,
            }
            for r in rows
        ]
        debug_path.write_text(json.dumps(debug_payload, ensure_ascii=False, indent=2), encoding="utf-8")

    # Return the file directly to support the simplest browser flow
    return FileResponse(
        path=str(xlsx_path),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        filename="extracted.xlsx",
        headers={"X-Download-URL": f"/download/{file_id}"},
    )

