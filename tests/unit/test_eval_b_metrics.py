
from pdftoxl.evals.metrics.eval_b import (
    BRANCHING_GRAMMAR,
    canonicalize_header,
    column_priority,
    compute_branching_syntactic_validity,
    compute_per_column_accuracy,
    compute_sequence_contiguity,
)
from pdftoxl.evals.workbook import Cell, SheetSnapshot, WorkbookSnapshot


def _mk_sheet(headers, rows, name="Assessment v2", header_row=13):
    cells = {}
    data_start = header_row + 1
    for ri, row in enumerate(rows):
        for ci, v in enumerate(row, start=1):
            cells[(data_start + ri, ci)] = Cell(row=data_start + ri, col=ci, value=v)
    return SheetSnapshot(
        name=name,
        header_row=header_row,
        headers=headers,
        data_start_row=data_start,
        data_end_row=data_start + len(rows) - 1 if rows else header_row,
        cells=cells,
    )


def _mk_wb(sheet):
    return WorkbookSnapshot(
        path="x.xlsx",
        sheet_names=[sheet.name],
        named_ranges=[],
        question_sheet=sheet,
    )


def test_canonicalize_header():
    assert canonicalize_header("Question Text") == "questiontext"
    assert canonicalize_header("Alert\n(Yes / No)") == "alertyesno"


def test_column_priority_lookup():
    assert column_priority("QuestionType") == "critical"
    assert column_priority("Branching Logic") == "high"
    assert column_priority("Auto Populate with:") == "medium"
    assert column_priority("Concept Code") == "low"
    assert column_priority("Mystery") == "unclassified"


def test_branching_grammar_positive():
    assert BRANCHING_GRAMMAR.match('Display if Q3 = "yes"')
    assert BRANCHING_GRAMMAR.match("If Q16 = checked(selected)")
    assert BRANCHING_GRAMMAR.match("Display if Q7 = Lives in own home")


def test_branching_grammar_negative():
    assert not BRANCHING_GRAMMAR.match("show when answered yes")
    assert not BRANCHING_GRAMMAR.match("Display if X1 = yes")
    assert not BRANCHING_GRAMMAR.match("Q3 = yes")


def test_compute_branching_syntactic_validity():
    sheet = _mk_sheet(
        ["Branching Logic"],
        [['Display if Q3 = "yes"'], [None], ["bad"]],
    )
    wb = _mk_wb(sheet)
    score, total = compute_branching_syntactic_validity(wb)
    assert total == 2
    assert abs(score - 0.5) < 1e-9


def test_compute_sequence_contiguity_ok():
    sheet = _mk_sheet(["Sequence"], [[1], [2], [3]])
    assert compute_sequence_contiguity(_mk_wb(sheet))


def test_compute_sequence_contiguity_gap():
    sheet = _mk_sheet(["Sequence"], [[1], [3]])
    assert not compute_sequence_contiguity(_mk_wb(sheet))


def test_compute_sequence_contiguity_empty():
    sheet = _mk_sheet(["Sequence"], [])
    assert compute_sequence_contiguity(_mk_wb(sheet))


def test_per_column_accuracy_self_compare():
    headers = ["QuestionType", "Question Text", "Sequence"]
    rows = [["Text Box", "What?", 1], ["Radio Button", "Why?", 2]]
    cand = _mk_wb(_mk_sheet(headers, rows))
    ref = _mk_wb(_mk_sheet(headers, rows))
    out = compute_per_column_accuracy(cand, ref)
    assert all(pc.accuracy == 1.0 for pc in out)
    assert all(pc.passed for pc in out)


def test_per_column_accuracy_mismatch():
    headers = ["QuestionType"]
    cand = _mk_wb(_mk_sheet(headers, [["Text Box"], ["Wrong"]]))
    ref = _mk_wb(_mk_sheet(headers, [["Text Box"], ["Radio Button"]]))
    out = compute_per_column_accuracy(cand, ref)
    assert out[0].accuracy == 0.5
    assert not out[0].passed


def test_low_priority_preserved_blank():
    headers = ["Concept Code"]
    cand = _mk_wb(_mk_sheet(headers, [[None], [""]]))
    ref = _mk_wb(_mk_sheet(headers, [[None], [None]]))
    out = compute_per_column_accuracy(cand, ref)
    assert out[0].priority == "low"
    assert out[0].accuracy == 1.0
    assert out[0].passed


def test_low_priority_not_blank_uses_cell_equality():
    headers = ["Concept Code"]
    cand = _mk_wb(_mk_sheet(headers, [["CC_1"], ["CC_2"]]))
    ref = _mk_wb(_mk_sheet(headers, [["CC_1"], ["CC_2"]]))
    out = compute_per_column_accuracy(cand, ref)
    assert out[0].priority == "low"
    assert out[0].accuracy == 1.0


def test_low_priority_fails_when_candidate_populates_blank_reference():
    headers = ["Concept Code"]
    cand = _mk_wb(_mk_sheet(headers, [["X"], ["Y"]]))
    ref = _mk_wb(_mk_sheet(headers, [[None], [None]]))
    out = compute_per_column_accuracy(cand, ref)
    assert out[0].priority == "low"
    assert out[0].accuracy == 0.0
    assert not out[0].passed
