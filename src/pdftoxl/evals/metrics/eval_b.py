from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from ..contracts import EvalResult, MetricResult
from ..normalize import cells_equal, controlled_vocab_contains, is_blank, normalize_string
from ..workbook import SheetSnapshot, WorkbookSnapshot, read_values_vocabulary

CRITICAL_COLUMNS = {"questiontype", "questiontext", "englishquestionindextext", "sequence"}
HIGH_COLUMNS = {
    "branchinglogic",
    "section",
    "englishsection",
    "required",
    "requiredyesno",
    "autopopulated",
    "autopopulatedyesno",
    "answervalidation",
    "answertext",
    "englishanswertext",
}
MEDIUM_COLUMNS = {
    "autopopulatewith",
    "autopopulatefield",
    "alert",
    "alertyesno",
    "alternatetext",
    "alternatetextyesno",
    "history",
    "historyyesno",
    "prepopulate",
    "prepopulateyesno",
    "speechtotext",
    "speechtotextyesno",
    "submissionhistory",
    "submissionhistoryyesno",
}
LOW_PRESERVED_BLANK = {
    "conceptcode",
    "conceptcodeformigrationuseonly",
    "tokenid",
    "tokenidpdfgeneration",
    "itnotes",
    "talkingpoints",
    "alerttype",
    "alerttext",
    "alternatequestiontext",
    "alternateanswertext",
}

YES_NO_COLUMNS = {
    "alert",
    "alertyesno",
    "alternatetext",
    "alternatetextyesno",
    "autopopulated",
    "autopopulatedyesno",
    "history",
    "historyyesno",
    "prepopulate",
    "prepopulateyesno",
    "required",
    "requiredyesno",
    "speechtotext",
    "speechtotextyesno",
    "submissionhistory",
    "submissionhistoryyesno",
}

CRITICAL_THRESHOLD = 0.97
HIGH_THRESHOLD = 0.92
MEDIUM_THRESHOLD = 0.85
LOW_THRESHOLD = 0.99

QUESTION_TYPE_HEADERS = {"questiontype"}
SEQUENCE_HEADERS = {"sequence"}
BRANCHING_HEADERS = {"branchinglogic"}

BRANCHING_GRAMMAR = re.compile(r"^(?:Display if|If)\s+Q\d+\b.*$", re.IGNORECASE)


def canonicalize_header(h: str) -> str:
    s = re.sub(r"\s+", "", h or "")
    s = re.sub(r"[^A-Za-z0-9]", "", s)
    return s.lower()


def column_priority(header: str) -> str:
    c = canonicalize_header(header)
    if c in CRITICAL_COLUMNS:
        return "critical"
    if c in HIGH_COLUMNS:
        return "high"
    if c in MEDIUM_COLUMNS:
        return "medium"
    if c in LOW_PRESERVED_BLANK:
        return "low"
    return "unclassified"


def priority_threshold(priority: str) -> float:
    return {
        "critical": CRITICAL_THRESHOLD,
        "high": HIGH_THRESHOLD,
        "medium": MEDIUM_THRESHOLD,
        "low": LOW_THRESHOLD,
    }.get(priority, 0.0)


@dataclass
class PerColumn:
    header: str
    canonical: str
    priority: str
    accuracy: float
    matched: int
    total: int
    threshold: float
    passed: bool


@dataclass
class EvalBMetrics:
    row_count_delta: int
    candidate_rows: int
    reference_rows: int
    per_column: list[PerColumn]
    controlled_vocab_validity: float
    out_of_vocab_count: int
    yes_no_validity: float
    branching_syntactic_validity: float
    branching_total: int
    sequence_contiguity: bool
    formatting_preservation: bool
    formatting_subchecks: dict[str, bool] = field(default_factory=dict)
    header_meta_accuracy: float = 1.0
    header_meta_details: dict[str, bool] = field(default_factory=dict)


def _aligned_row_range(cand: SheetSnapshot, ref: SheetSnapshot) -> int:
    return min(cand.row_count, ref.row_count)


def _ref_column_all_blank(sheet: SheetSnapshot, col: int) -> bool:
    for r in range(sheet.data_start_row, sheet.data_end_row + 1):
        c = sheet.cells.get((r, col))
        if not is_blank(c.value if c else None):
            return False
    return True


def _row_match(cand_sheet: SheetSnapshot, ref_sheet: SheetSnapshot, col_cand: int, col_ref: int) -> tuple[int, int]:
    n = _aligned_row_range(cand_sheet, ref_sheet)
    matched = 0
    for i in range(n):
        r_c = cand_sheet.data_start_row + i
        r_r = ref_sheet.data_start_row + i
        cv = cand_sheet.cells.get((r_c, col_cand))
        rv = ref_sheet.cells.get((r_r, col_ref))
        a = cv.value if cv else None
        b = rv.value if rv else None
        if cells_equal(a, b):
            matched += 1
    return matched, n


def compute_per_column_accuracy(
    candidate: WorkbookSnapshot, reference: WorkbookSnapshot
) -> list[PerColumn]:
    results: list[PerColumn] = []
    cand_sheet = candidate.question_sheet
    ref_sheet = reference.question_sheet
    cand_headers_canon = {canonicalize_header(h): i + 1 for i, h in enumerate(cand_sheet.headers)}
    for idx, ref_header in enumerate(ref_sheet.headers, start=1):
        canon = canonicalize_header(ref_header)
        if canon == "":
            continue
        col_cand = cand_headers_canon.get(canon)
        priority = column_priority(ref_header)
        if col_cand is None:
            n = ref_sheet.row_count
            acc = 0.0
            matched = 0
            total = n
        else:
            if priority == "low" and _ref_column_all_blank(ref_sheet, idx):
                total = ref_sheet.row_count
                matched = 0
                for i in range(total):
                    r_c = cand_sheet.data_start_row + i
                    cv = cand_sheet.cells.get((r_c, col_cand))
                    if is_blank(cv.value if cv else None):
                        matched += 1
                acc = matched / total if total else 1.0
            else:
                matched, total = _row_match(cand_sheet, ref_sheet, col_cand, idx)
                acc = matched / total if total else 1.0
        threshold = priority_threshold(priority)
        passed = priority == "unclassified" or acc >= threshold
        results.append(
            PerColumn(
                header=ref_header,
                canonical=canon,
                priority=priority,
                accuracy=acc,
                matched=matched,
                total=total,
                threshold=threshold,
                passed=passed,
            )
        )
    return results


def compute_row_count_delta(candidate: WorkbookSnapshot, reference: WorkbookSnapshot) -> int:
    return candidate.question_sheet.row_count - reference.question_sheet.row_count


def _find_column(sheet: SheetSnapshot, canon_targets: set[str]) -> int | None:
    for i, h in enumerate(sheet.headers, start=1):
        if canonicalize_header(h) in canon_targets:
            return i
    return None


def compute_controlled_vocab_validity(
    candidate: WorkbookSnapshot,
    vocab: list[str],
    reference: WorkbookSnapshot | None = None,
) -> tuple[float, int]:
    sheet = candidate.question_sheet
    col = _find_column(sheet, QUESTION_TYPE_HEADERS)
    if col is None:
        return 1.0, 0
    ref_sheet = reference.question_sheet if reference else None
    ref_col = _find_column(ref_sheet, QUESTION_TYPE_HEADERS) if ref_sheet else None
    total = 0
    valid = 0
    oov = 0
    for i, r in enumerate(range(sheet.data_start_row, sheet.data_end_row + 1)):
        cell = sheet.cells.get((r, col))
        v = cell.value if cell else None
        if is_blank(v):
            continue
        total += 1
        if controlled_vocab_contains(vocab, v):
            valid += 1
            continue
        if ref_sheet is not None and ref_col is not None:
            rr = ref_sheet.data_start_row + i
            rc = ref_sheet.cells.get((rr, ref_col))
            if cells_equal(v, rc.value if rc else None):
                valid += 1
                continue
        oov += 1
    return (valid / total if total else 1.0), oov


def compute_yes_no_validity(candidate: WorkbookSnapshot) -> float:
    sheet = candidate.question_sheet
    allowed = {"yes", "no"}
    total = 0
    valid = 0
    for i, h in enumerate(sheet.headers, start=1):
        if canonicalize_header(h) not in YES_NO_COLUMNS:
            continue
        for r in range(sheet.data_start_row, sheet.data_end_row + 1):
            cell = sheet.cells.get((r, i))
            v = cell.value if cell else None
            if is_blank(v):
                continue
            total += 1
            if normalize_string(v).casefold() in allowed:
                valid += 1
    return valid / total if total else 1.0


def compute_branching_syntactic_validity(candidate: WorkbookSnapshot) -> tuple[float, int]:
    sheet = candidate.question_sheet
    col = _find_column(sheet, BRANCHING_HEADERS)
    if col is None:
        return 1.0, 0
    total = 0
    ok = 0
    for r in range(sheet.data_start_row, sheet.data_end_row + 1):
        cell = sheet.cells.get((r, col))
        v = cell.value if cell else None
        if is_blank(v):
            continue
        total += 1
        s = normalize_string(v)
        if BRANCHING_GRAMMAR.match(s):
            ok += 1
    return (ok / total if total else 1.0), total


def compute_sequence_contiguity(candidate: WorkbookSnapshot) -> bool:
    sheet = candidate.question_sheet
    col = _find_column(sheet, SEQUENCE_HEADERS)
    if col is None:
        return True
    seqs: list[int] = []
    for r in range(sheet.data_start_row, sheet.data_end_row + 1):
        cell = sheet.cells.get((r, col))
        v = cell.value if cell else None
        if is_blank(v):
            continue
        try:
            seqs.append(int(float(str(v))))
        except (ValueError, TypeError):
            return False
    if not seqs:
        return True
    return sorted(seqs) == list(range(1, len(seqs) + 1))


def compute_formatting_preservation(
    candidate: WorkbookSnapshot, reference: WorkbookSnapshot
) -> tuple[bool, dict[str, bool]]:
    sub: dict[str, bool] = {}
    sub["sheet_names_match"] = sorted(candidate.sheet_names) == sorted(reference.sheet_names)
    sub["frozen_panes_match"] = (
        candidate.question_sheet.frozen_panes == reference.question_sheet.frozen_panes
    )
    sub["data_validations_present"] = (
        len(candidate.question_sheet.data_validations) >= len(reference.question_sheet.data_validations)
    )
    sub["merged_ranges_match"] = sorted(candidate.question_sheet.merged_ranges) == sorted(
        reference.question_sheet.merged_ranges
    )
    sub["named_ranges_match"] = sorted(candidate.defined_names) == sorted(reference.defined_names)
    return all(sub.values()), sub


def compute_header_meta_accuracy(
    candidate: WorkbookSnapshot, reference: WorkbookSnapshot
) -> tuple[float, dict[str, bool]]:
    details: dict[str, bool] = {}
    cand = candidate.question_sheet
    ref = reference.question_sheet
    total = 0
    matched = 0
    for r in range(1, ref.header_row):
        for c in range(1, len(ref.headers) + 1):
            rv = ref.cells.get((r, c))
            cv = cand.cells.get((r, c))
            a = cv.value if cv else None
            b = rv.value if rv else None
            if is_blank(a) and is_blank(b):
                continue
            total += 1
            eq = cells_equal(a, b)
            if eq:
                matched += 1
            details[f"R{r}C{c}"] = eq
    acc = matched / total if total else 1.0
    return acc, details


def evaluate_workbook(
    candidate: WorkbookSnapshot,
    reference: WorkbookSnapshot,
    vocab: list[str] | None = None,
) -> EvalBMetrics:
    if vocab is None:
        vocab = read_values_vocabulary(reference.path)
    per_col = compute_per_column_accuracy(candidate, reference)
    cv_valid, oov = compute_controlled_vocab_validity(candidate, vocab, reference)
    yn_valid = compute_yes_no_validity(candidate)
    branching, branching_total = compute_branching_syntactic_validity(candidate)
    seq_ok = compute_sequence_contiguity(candidate)
    fmt_ok, fmt_sub = compute_formatting_preservation(candidate, reference)
    meta_acc, meta_details = compute_header_meta_accuracy(candidate, reference)
    return EvalBMetrics(
        row_count_delta=compute_row_count_delta(candidate, reference),
        candidate_rows=candidate.question_sheet.row_count,
        reference_rows=reference.question_sheet.row_count,
        per_column=per_col,
        controlled_vocab_validity=cv_valid,
        out_of_vocab_count=oov,
        yes_no_validity=yn_valid,
        branching_syntactic_validity=branching,
        branching_total=branching_total,
        sequence_contiguity=seq_ok,
        formatting_preservation=fmt_ok,
        formatting_subchecks=fmt_sub,
        header_meta_accuracy=meta_acc,
        header_meta_details=meta_details,
    )


def metrics_to_eval_result(fixture_id: str, m: EvalBMetrics) -> EvalResult:
    crit_pass = all(pc.passed for pc in m.per_column if pc.priority == "critical")
    high_pass = all(pc.passed for pc in m.per_column if pc.priority == "high")
    medium_pass = all(pc.passed for pc in m.per_column if pc.priority == "medium")
    low_pass = all(pc.passed for pc in m.per_column if pc.priority == "low")
    row_delta_ok = abs(m.row_count_delta) <= 2
    cv_ok = m.controlled_vocab_validity >= 1.0
    branching_ok = m.branching_syntactic_validity >= 0.97
    fixture_pass = (
        crit_pass
        and high_pass
        and medium_pass
        and low_pass
        and row_delta_ok
        and cv_ok
        and branching_ok
        and m.sequence_contiguity
        and m.formatting_preservation
    )
    metrics: list[MetricResult] = [
        MetricResult(
            name="row_count_delta",
            value=m.row_count_delta,
            details={"candidate": m.candidate_rows, "reference": m.reference_rows},
            passed=row_delta_ok,
        ),
        MetricResult(
            name="controlled_vocab_validity",
            value=m.controlled_vocab_validity,
            details={"out_of_vocab": m.out_of_vocab_count},
            passed=cv_ok,
        ),
        MetricResult(
            name="yes_no_validity",
            value=m.yes_no_validity,
            passed=m.yes_no_validity >= 1.0,
        ),
        MetricResult(
            name="branching_syntactic_validity",
            value=m.branching_syntactic_validity,
            details={"total_with_branching": m.branching_total},
            passed=branching_ok,
        ),
        MetricResult(
            name="sequence_contiguity",
            value=bool(m.sequence_contiguity),
            passed=m.sequence_contiguity,
        ),
        MetricResult(
            name="formatting_preservation",
            value=bool(m.formatting_preservation),
            details={k: bool(v) for k, v in m.formatting_subchecks.items()},
            passed=m.formatting_preservation,
        ),
        MetricResult(
            name="header_meta_accuracy",
            value=m.header_meta_accuracy,
            details={"sub_count": len(m.header_meta_details)},
            passed=None,
        ),
    ]
    for pc in m.per_column:
        metrics.append(
            MetricResult(
                name=f"per_column_accuracy[{pc.header}]",
                value=pc.accuracy,
                details={
                    "priority": pc.priority,
                    "matched": pc.matched,
                    "total": pc.total,
                    "threshold": pc.threshold,
                },
                passed=pc.passed if pc.priority != "unclassified" else None,
            )
        )
    return EvalResult(fixture_id=fixture_id, eval_name="B", metrics=metrics, passed=fixture_pass)


def load_and_evaluate(
    candidate_path: Any,
    reference_path: Any,
    question_sheet: str,
    header_row: int,
    fixture_id: str,
) -> tuple[EvalResult, EvalBMetrics]:
    from ..workbook import read_workbook

    cand = read_workbook(candidate_path, question_sheet, header_row)
    ref = read_workbook(reference_path, question_sheet, header_row)
    metrics = evaluate_workbook(cand, ref)
    result = metrics_to_eval_result(fixture_id, metrics)
    return result, metrics
