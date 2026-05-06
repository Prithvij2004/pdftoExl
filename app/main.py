from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    def _load_env_file(path: Path) -> None:
        if not path.exists():
            return
        for raw in path.read_text().splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            key, val = key.strip(), val.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = val
    _load_env_file(Path(__file__).resolve().parent.parent / ".env")

from starlette.background import BackgroundTask
from starlette.concurrency import run_in_threadpool
from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse

from app.config import load_pipeline_config
from app.observability import configure_logfire
from app.services.pipeline import CurrentExtractionPipeline

configure_logfire(service_name="pdftoExl-api")

try:
    import logfire
except ImportError:
    logfire = None  # type: ignore[assignment]


app = FastAPI(title="PDF to Excel Extractor")

if logfire is not None:
    logfire.instrument_fastapi(app, capture_headers=False)

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

    tmp_path = Path(tempfile.mkdtemp(prefix="pdf-extract-"))
    pdf_path = tmp_path / (Path(file.filename or "form.pdf").stem + ".pdf")
    xlsx_path = tmp_path / (pdf_path.stem + ".xlsx")

    try:
        pdf_bytes = await file.read()
        pdf_path.write_bytes(pdf_bytes)
        span_ctx = (
            logfire.span(
                "extract_pdf",
                filename=file.filename,
                pdf_bytes=len(pdf_bytes),
            )
            if logfire is not None
            else None
        )
        def _run() -> object:
            pipeline = CurrentExtractionPipeline(load_pipeline_config())
            return pipeline.extract_to_workbook(pdf_path, xlsx_path)

        if span_ctx is not None:
            with span_ctx as span:
                outcome = await run_in_threadpool(_run)
                span.set_attribute("rows", len(outcome.final_rows))
                span.set_attribute("chunks", len(outcome.run_result.chunks))
                span.set_attribute("pages", len(outcome.run_result.semantic_pages))
        else:
            await run_in_threadpool(_run)
    except Exception as exc:
        shutil.rmtree(tmp_path, ignore_errors=True)
        raise HTTPException(status_code=502, detail=f"Extraction failed: {exc}") from exc

    return FileResponse(
        path=xlsx_path,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        filename=f"{pdf_path.stem}.xlsx",
        background=BackgroundTask(shutil.rmtree, tmp_path, ignore_errors=True),
    )
