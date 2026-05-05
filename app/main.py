from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path

from starlette.background import BackgroundTask
from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse

from extract_form import post_process, run_extract, write_xlsx


app = FastAPI(title="PDF to Excel Extractor")

app.add_middleware(
    CORSMiddleware,
    allow_origins=os.environ.get("CORS_ORIGINS", "http://localhost:5173").split(","),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["Content-Disposition"],
)


@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/api/extract")
async def extract_pdf(file: UploadFile = File(...)) -> FileResponse:
    if file.content_type not in {"application/pdf", "application/x-pdf"}:
        raise HTTPException(status_code=400, detail="Upload a PDF file.")

    api_key = os.environ.get("LLAMA_CLOUD_API_KEY")
    if not api_key:
        raise HTTPException(status_code=500, detail="LLAMA_CLOUD_API_KEY is not configured.")

    tmp_path = Path(tempfile.mkdtemp(prefix="pdf-extract-"))
    pdf_path = tmp_path / (Path(file.filename or "form.pdf").stem + ".pdf")
    xlsx_path = tmp_path / (pdf_path.stem + ".xlsx")

    try:
        pdf_path.write_bytes(await file.read())
        form = post_process(run_extract(pdf_path, api_key))
        write_xlsx(form, xlsx_path)
    except Exception as exc:
        shutil.rmtree(tmp_path, ignore_errors=True)
        raise HTTPException(status_code=502, detail=f"Extraction failed: {exc}") from exc

    return FileResponse(
        path=xlsx_path,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        filename=f"{pdf_path.stem}.xlsx",
        background=BackgroundTask(shutil.rmtree, tmp_path, ignore_errors=True),
    )
