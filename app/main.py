from __future__ import annotations

import os
from pathlib import Path

import anyio
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates

from app.config import settings
from app.pipeline.pdf_to_excel import convert_pdf_to_excel
from app.pipeline.pdf_to_markdown import convert_pdf_to_markdown


app = FastAPI(title="PDF to Excel Extractor", version="0.1.0")
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

PDF_MIME_TYPES = {"application/pdf", "application/x-pdf"}


@app.on_event("startup")
def _startup() -> None:
    settings.runtime_dir.mkdir(parents=True, exist_ok=True)
    settings.uploads_dir.mkdir(parents=True, exist_ok=True)
    settings.generated_dir.mkdir(parents=True, exist_ok=True)
    settings.output_dir.mkdir(parents=True, exist_ok=True)
    settings.downloads_dir.mkdir(parents=True, exist_ok=True)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/", response_class=HTMLResponse)
def index(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(request, "index.html")


def _safe_filename(name: str) -> str:
    base = os.path.basename(name or "upload.pdf")
    base = base.replace("\x00", "")
    base = "".join(ch if ch not in '<>:"/\\|?*' else "_" for ch in base).strip()
    if not base.lower().endswith(".pdf"):
        base += ".pdf"
    return base[:180]


async def _persist_upload_pdf(file: UploadFile) -> tuple[str, Path]:
    if file.content_type not in PDF_MIME_TYPES:
        raise HTTPException(status_code=400, detail="Only PDF uploads are supported.")

    original_name = _safe_filename(file.filename or "upload.pdf")
    file_id = Path(original_name).stem
    out_path = settings.uploads_dir / original_name

    max_bytes = settings.max_upload_mb * 1024 * 1024
    total = 0

    with out_path.open("wb") as f:
        while True:
            chunk = await file.read(1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > max_bytes:
                out_path.unlink(missing_ok=True)
                raise HTTPException(
                    status_code=413,
                    detail=f"File too large. Max upload is {settings.max_upload_mb} MB.",
                )
            f.write(chunk)

    with out_path.open("rb") as f:
        if f.read(5) != b"%PDF-":
            out_path.unlink(missing_ok=True)
            raise HTTPException(status_code=400, detail="Uploaded file is not a valid PDF.")

    return file_id, out_path


@app.get("/download/{file_id}")
def download(file_id: str):
    xlsx_path = settings.downloads_dir / f"{file_id}_generated.xlsx"
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
        result = await anyio.to_thread.run_sync(convert_pdf_to_excel, pdf_path)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e

    markdown = result.markdown.markdown_path.read_text(encoding="utf-8")
    return JSONResponse(
        {
            "status": "success",
            "parser": result.markdown.parser,
            "markdown_path": str(result.markdown.markdown_path),
            "metadata_path": str(result.markdown.metadata_path),
            "page_count": result.markdown.page_count,
            "preview": markdown[:1000],
            "file_id": file_id,
            "excel_path": str(result.excel.excel_path),
            "excel_download_url": f"/download/excel/{result.excel.excel_path.name}",
            "validation_issues_path": str(result.excel.validation_issues_path),
            "review_report_path": str(result.excel.review_report_path),
            "needs_review_count": result.excel.needs_review_count,
        }
    )


@app.post("/parse/markdown")
async def parse_markdown(file: UploadFile = File(...), parser: str | None = Form(default=None)):
    file_id, pdf_path = await _persist_upload_pdf(file)

    try:
        result = await anyio.to_thread.run_sync(convert_pdf_to_markdown, pdf_path, parser)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e

    return {
        "status": result.status,
        "parser": result.parser,
        "markdown_path": str(result.markdown_path),
        "metadata_path": str(result.metadata_path),
        "page_count": result.page_count,
        "file_id": file_id,
    }


@app.post("/convert/pdf-to-excel")
async def convert_pdf_to_excel_endpoint(file: UploadFile = File(...), parser: str | None = Form(default=None)):
    file_id, pdf_path = await _persist_upload_pdf(file)

    try:
        result = await anyio.to_thread.run_sync(convert_pdf_to_excel, pdf_path, parser)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e

    return {
        "status": "success",
        "excel_path": str(result.excel.excel_path),
        "excel_download_url": f"/download/excel/{result.excel.excel_path.name}",
        "markdown_path": str(result.markdown.markdown_path),
        "validation_issues_path": str(result.excel.validation_issues_path),
        "needs_review_count": result.excel.needs_review_count,
        "file_id": file_id,
    }


@app.get("/download/excel/{filename}")
def download_excel(filename: str):
    safe_name = os.path.basename(filename)
    xlsx_path = settings.downloads_dir / safe_name
    if not xlsx_path.exists():
        raise HTTPException(status_code=404, detail="File not found.")
    return FileResponse(
        path=str(xlsx_path),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        filename=filename,
    )
