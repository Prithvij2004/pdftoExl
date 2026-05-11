from __future__ import annotations

import re

from app.schemas.form_schema import DocumentExtraction, FormItem
from app.validation.validation_models import ValidationIssue


DATE_RE = re.compile(r"\b(date|dob|birth|admit|admission|discharge|fall)\b", re.IGNORECASE)
SIGNATURE_RE = re.compile(r"\b(signature|sign|signed)\b", re.IGNORECASE)
NUMBER_RE = re.compile(r"\b(score|count|number|#|amount|age|fall #)\b", re.IGNORECASE)
BUSINESS_RE = re.compile(r"\b(auto[- ]?populate|pre[- ]?populate|concept code|token id|migration)\b", re.IGNORECASE)
HEADER_FOOTER_RE = re.compile(r"^\s*(page\s+\d+|tc\d+|rda\s+\d+|\d+\s+safety determination request form)", re.IGNORECASE)


def _issue(severity: str, message: str, item_id: str | None = None, action: str = "") -> ValidationIssue:
    return ValidationIssue(
        severity=severity,  # type: ignore[arg-type]
        item_id=item_id,
        message=message,
        suggested_action=action,
    )


def _mark_review(item: FormItem, reason: str) -> FormItem:
    review_reason = item.review_reason
    if reason and reason not in review_reason:
        review_reason = f"{review_reason}; {reason}".strip("; ")
    return item.model_copy(update={"needs_review": True, "review_reason": review_reason})


def _blob(item: FormItem) -> str:
    return " ".join([item.question_text, item.display_text, item.source_text])


def validate_document(doc: DocumentExtraction) -> tuple[DocumentExtraction, list[ValidationIssue]]:
    issues: list[ValidationIssue] = []
    if not doc.sections:
        return doc, [_issue("error", "Document has no sections", action="Re-run extraction")]

    validated_sections = []
    for section in doc.sections:
        if not section.items:
            issues.append(_issue("warning", f"Section has no items: {section.section_title}", action="Review section"))

        items = []
        for item in section.items:
            current = item
            text = _blob(current)

            if current.confidence < 0.75:
                current = _mark_review(current, "Confidence below 0.75")
                issues.append(_issue("warning", "Item confidence is below 0.75", current.item_id, "Review item"))

            if current.item_type == "radio_group" and len(current.options) < 2:
                current = _mark_review(current, "Radio group has fewer than two options")
                issues.append(_issue("warning", "Radio group must have 2+ options", current.item_id, "Review grouping"))

            if current.item_type == "dropdown" and not current.options:
                current = _mark_review(current, "Dropdown has no options")
                issues.append(_issue("warning", "Dropdown must have options", current.item_id, "Review options"))

            if current.item_type == "checkbox_item" and current.options:
                current = _mark_review(current, "Checkbox item unexpectedly has options")
                issues.append(_issue("info", "Checkbox item has options", current.item_id, "Confirm modeling"))

            if current.item_type == "table" and (not current.table or not current.table.columns):
                current = _mark_review(current, "Table has no columns")
                issues.append(_issue("warning", "Table item must have columns", current.item_id, "Review table"))

            if current.item_type == "date" and not DATE_RE.search(text):
                current = _mark_review(current, "Date label is not date-like")
                issues.append(_issue("warning", "Date item label is not date-like", current.item_id, "Review type"))

            if current.item_type == "signature" and not SIGNATURE_RE.search(text):
                current = _mark_review(current, "Signature label is not signature-like")
                issues.append(_issue("warning", "Signature item label is not signature-like", current.item_id, "Review type"))

            if current.item_type == "number" and not NUMBER_RE.search(text):
                current = _mark_review(current, "Number label is not number-like")
                issues.append(_issue("warning", "Number item label is not number-like", current.item_id, "Review type"))

            if current.item_type == "review_required" and not current.needs_review:
                current = _mark_review(current, "review_required item type")
                issues.append(_issue("warning", "review_required item must have needs_review=true", current.item_id))

            if BUSINESS_RE.search(text):
                current = _mark_review(current, "Business/system field detected")
                issues.append(_issue("warning", "Business/system-only field detected", current.item_id, "Remove if not visible"))

            if HEADER_FOOTER_RE.search(text):
                current = _mark_review(current, "Possible repeated header/footer")
                issues.append(_issue("info", "Possible repeated page header/footer", current.item_id, "Review output"))

            for followup in current.followups:
                if not followup.trigger.strip():
                    current = _mark_review(current, "Followup trigger is blank")
                    issues.append(_issue("warning", "Followup trigger must not be blank", followup.followup_id))
                if not followup.source_pages:
                    issues.append(_issue("info", "Followup has no source_pages", followup.followup_id))

            items.append(current)

        validated_sections.append(section.model_copy(update={"items": items}))

    validated = doc.model_copy(update={"sections": validated_sections})
    issues.extend(_choices_reference_checks(validated))
    return validated, issues


def _choices_reference_checks(doc: DocumentExtraction) -> list[ValidationIssue]:
    blob = " ".join(
        [doc.document_title, doc.form_name]
        + [section.section_title for section in doc.sections]
        + [item.question_text for section in doc.sections for item in section.items]
    ).lower()
    if "choices" not in blob and "safety determination" not in blob:
        return []

    expected = [
        ("Applicant Name", "Applicant Name exists"),
        ("SSN", "SSN exists"),
        ("DOB", "DOB exists"),
        ("Total Acuity Score", "Total Acuity Score exists"),
        ("Current Living Arrangements", "Current Living Arrangements section exists"),
        ("Applicant residence", "Applicant residence exists"),
        ("Justification", "Justification section exists"),
        ("Description of documentation attached", "Documentation attached fields exist"),
        ("Additional Required Documentation", "Additional Required Documentation exists"),
        ("Submitting Entity Attestation", "Submitting Entity Attestation exists"),
        ("Signature", "Signature field exists"),
        ("Fall", "Fall form/table exists if present"),
    ]

    issues: list[ValidationIssue] = []
    searchable = blob.lower()
    for needle, message in expected:
        if needle.lower() not in searchable:
            issues.append(
                _issue(
                    "warning",
                    f"CHOICES reference check missing: {message}",
                    action="Review extraction against CHOICES reference workbook",
                )
            )
    return issues
