"""
Stages 1-5: probe + rasterize + AcroForm widgets + PyMuPDF text-layout + Qwen3-VL VLM extraction.

One Bedrock Converse call per page (Qwen3-VL caps at ~5 images, but to keep prompts focused
and stay under output-token caps we batch one page per call). Structural hints (widgets +
layout text) are passed alongside the page image so the model has both modalities.
"""
from __future__ import annotations
import io, json, os, time, re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import fitz  # PyMuPDF
import boto3
from botocore.config import Config

from .schema import Row, EXTRACTION_SCHEMA_DESCRIPTION


# ---------- Probe ---------------------------------------------------------

@dataclass
class PageStructure:
    page_index: int                          # 0-based
    width: float
    height: float
    image_path: str
    widgets: list[dict] = field(default_factory=list)
    text_blocks: list[dict] = field(default_factory=list)


@dataclass
class DocStructure:
    pdf_path: str
    page_count: int
    has_acroform: bool
    pages: list[PageStructure] = field(default_factory=list)


def probe_and_rasterize(pdf_path: str, image_dir: str, dpi: int = 200) -> DocStructure:
    Path(image_dir).mkdir(parents=True, exist_ok=True)
    doc = fitz.open(pdf_path)
    pages: list[PageStructure] = []
    has_widgets = False
    stem = Path(pdf_path).stem.replace(" ", "_")[:40]
    for i, page in enumerate(doc):
        img_path = os.path.join(image_dir, f"{stem}_p{i+1:02d}.png")
        if not os.path.exists(img_path):
            pix = page.get_pixmap(dpi=dpi)
            pix.save(img_path)

        widgets: list[dict] = []
        for w in (page.widgets() or []):
            r = w.rect
            widgets.append({
                "name": w.field_name,
                "label": w.field_label or "",
                "type": w.field_type_string,
                "value": w.field_value or "",
                "rect": [round(r.x0, 2), round(r.y0, 2), round(r.x1, 2), round(r.y1, 2)],
            })
        if widgets:
            has_widgets = True

        text_blocks: list[dict] = []
        for blk in page.get_text("dict")["blocks"]:
            if blk.get("type") != 0:
                continue
            for line in blk.get("lines", []):
                line_text = " ".join(span["text"] for span in line["spans"]).strip()
                if not line_text:
                    continue
                bb = line["bbox"]
                avg_size = sum(s["size"] for s in line["spans"]) / max(1, len(line["spans"]))
                text_blocks.append({
                    "text": line_text,
                    "rect": [round(bb[0], 2), round(bb[1], 2), round(bb[2], 2), round(bb[3], 2)],
                    "size": round(avg_size, 1),
                })

        pages.append(PageStructure(
            page_index=i,
            width=page.rect.width,
            height=page.rect.height,
            image_path=img_path,
            widgets=widgets,
            text_blocks=text_blocks,
        ))
    doc.close()
    return DocStructure(
        pdf_path=pdf_path,
        page_count=len(pages),
        has_acroform=has_widgets,
        pages=pages,
    )


# ---------- Bedrock Qwen3-VL ---------------------------------------------

def _bedrock_runtime():
    cfg = Config(read_timeout=300, retries={"max_attempts": 3, "mode": "standard"})
    return boto3.client("bedrock-runtime",
                        region_name=os.environ.get("AWS_REGION", "us-west-2"),
                        config=cfg)


def _build_prompt(page_struct: PageStructure, doc_struct: DocStructure) -> str:
    parts = [
        "You are a document-extraction expert reading one page of a healthcare/insurance "
        "form PDF. Your job is to produce a structured row per visible question/section/"
        "static-text artifact, matching the EAB 28-column Assessment Template.",
        "",
        f"This is page {page_struct.page_index + 1} of {doc_struct.page_count}.",
    ]
    if doc_struct.has_acroform and page_struct.widgets:
        parts.append("")
        parts.append("== AcroForm widgets present on this page (authoritative, use these) ==")
        for w in page_struct.widgets[:60]:
            parts.append(f"  - type={w['type']:<10} name={w['name']!r:<60}  label={w['label']!r}  rect={w['rect']}")
    elif doc_struct.has_acroform:
        parts.append("")
        parts.append("(This PDF has AcroForm widgets but none on this page.)")
    else:
        parts.append("")
        parts.append("(This PDF has NO AcroForm widgets — infer field types from visual cues: "
                     "underlines for Text Box, longer multi-line underlines for Text Area, "
                     "checkbox glyphs for Checkbox, ruled grids for Group Table.)")

    if page_struct.text_blocks:
        parts.append("")
        parts.append("== Text layout (PyMuPDF lines, top-to-bottom, may help disambiguate sections/labels) ==")
        for tb in page_struct.text_blocks[:80]:
            parts.append(f"  [{tb['rect'][0]:>5.0f},{tb['rect'][1]:>5.0f}] sz{tb['size']:>4} {tb['text'][:120]!r}")

    parts.append("")
    parts.append("== Output schema ==")
    parts.append(EXTRACTION_SCHEMA_DESCRIPTION)

    parts.append("")
    parts.append("Produce one JSON array element per artifact on this page in reading order. "
                 "If the page contains 30 artifacts (questions + section banners + instructional "
                 "Display paragraphs), produce 30 elements. Output ONLY the JSON array.")
    return "\n".join(parts)


def _strip_codefence(s: str) -> str:
    s = s.strip()
    if s.startswith("```"):
        s = re.sub(r"^```(?:json)?\s*", "", s)
        s = re.sub(r"\s*```$", "", s)
    return s.strip()


def extract_page_with_qwen(client, page_struct: PageStructure, doc_struct: DocStructure,
                           model_id: str, max_tokens: int = 8000,
                           verbose: bool = True) -> tuple[list[dict], dict]:
    """Returns (rows_dict_list, telemetry). Each rows-dict has keys defined in EXTRACTION_SCHEMA_DESCRIPTION."""
    img_bytes = Path(page_struct.image_path).read_bytes()
    prompt = _build_prompt(page_struct, doc_struct)
    t0 = time.time()
    resp = client.converse(
        modelId=model_id,
        messages=[{"role": "user", "content": [
            {"image": {"format": "png", "source": {"bytes": img_bytes}}},
            {"text": prompt},
        ]}],
        inferenceConfig={"maxTokens": max_tokens, "temperature": 0.0},
    )
    elapsed = time.time() - t0
    raw = resp["output"]["message"]["content"][0]["text"]
    usage = resp.get("usage", {})
    cleaned = _strip_codefence(raw)
    try:
        parsed = json.loads(cleaned)
        if not isinstance(parsed, list):
            raise ValueError(f"Expected list, got {type(parsed).__name__}")
    except Exception as e:
        # Salvage: extract first array
        m = re.search(r"\[.*\]", cleaned, flags=re.S)
        parsed = json.loads(m.group(0)) if m else []
        if verbose:
            print(f"   [warn] page {page_struct.page_index+1}: JSON salvage applied ({e})")
    if verbose:
        print(f"   page {page_struct.page_index+1}: {len(parsed)} rows  "
              f"({elapsed:.1f}s, in={usage.get('inputTokens')}, out={usage.get('outputTokens')})")
    telemetry = {
        "page": page_struct.page_index + 1,
        "elapsed_s": round(elapsed, 2),
        "input_tokens": usage.get("inputTokens"),
        "output_tokens": usage.get("outputTokens"),
        "row_count": len(parsed),
    }
    return parsed, telemetry


def extract_document(doc_struct: DocStructure, model_id: str, verbose: bool = True
                     ) -> tuple[list[dict], list[dict]]:
    client = _bedrock_runtime()
    all_rows: list[dict] = []
    telemetry: list[dict] = []
    for page_struct in doc_struct.pages:
        rows, tele = extract_page_with_qwen(client, page_struct, doc_struct, model_id, verbose=verbose)
        for r in rows:
            r["_page"] = page_struct.page_index + 1
        all_rows.extend(rows)
        telemetry.append(tele)
    return all_rows, telemetry


# ---------- Convert raw VLM dict → Row -----------------------------------

_VLM_KEY_TO_ROW_FIELD = {
    "section": "section",
    "alert": "alert",
    "alternate_text": "alternate_text",
    "auto_populated": "auto_populated",
    "history": "history",
    "pre_populate": "pre_populate",
    "required": "required",
    "speech_to_text": "speech_to_text",
    "submission_history": "submission_history",
    "concept_code": "concept_code",
    "sequence": "sequence",
    "question_rule": "question_rule",
    "question_type": "question_type",
    "question_text": "question_text",
    "branching_logic": "branching_logic",
    "answer_text": "answer_text",
    "answer_validation": "answer_validation",
    "answer_score_value": "answer_score_value",
    "talking_points": "talking_points",
    "auto_populate_with": "auto_populate_with",
    "auto_populate_field": "auto_populate_field",
    "auto_populate_rule": "auto_populate_rule",
    "alt_question_text": "alt_question_text",
    "alt_answer_text": "alt_answer_text",
    "alert_type": "alert_type",
    "alert_text": "alert_text",
    "token_id": "token_id",
    "it_notes": "it_notes",
}


def vlm_dicts_to_rows(raw: list[dict]) -> list[Row]:
    rows: list[Row] = []
    for d in raw:
        r = Row()
        for k, field_name in _VLM_KEY_TO_ROW_FIELD.items():
            if k in d and d[k] is not None:
                v = d[k]
                if field_name == "sequence":
                    try:
                        r.sequence = int(v) if v != "" else None
                    except (TypeError, ValueError):
                        r.sequence = None
                else:
                    setattr(r, field_name, str(v).strip() if not isinstance(v, str) else v.strip())
        page = d.get("_page") or d.get("page")
        if page is not None:
            try:
                r.page = int(page)
            except Exception:
                pass
        rows.append(r)
    return rows
