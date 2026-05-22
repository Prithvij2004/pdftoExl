from __future__ import annotations

import sys
from pathlib import Path

SHOWLAY_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SHOWLAY_DIR))

from showlay.postprocess import dedupe_table_repetitions  # noqa: E402
from showlay.schema import Row  # noqa: E402


def _row(question_type: str, question_text: str, page: int = 1) -> Row:
    return Row(page=page, question_type=question_type, question_text=question_text)


def test_repeated_table_instances_keep_same_labels_with_different_context():
    rows = [
        _row("Group Table", "Fall #\nDate of fall:\nLocation of Fall:", page=10),
        _row("Group Table", "Fall #\nDate of fall:\nLocation of Fall:", page=10),
        _row("Group Table", "Fall #\nDate of fall:\nLocation of Fall:", page=10),
    ]

    result = dedupe_table_repetitions(rows)

    assert len(result) == 3
    assert sum("Date of fall:" in row.question_text for row in result) == 3


def test_repeated_table_continuation_rows_are_not_dropped_as_duplicates():
    rows = [
        _row("Group Table", "Fall #\nDate of fall:\nLocation of Fall:", page=10),
        _row("Group Table", "Fall #\nDate of fall:\nLocation of Fall:", page=10),
        _row("Text Area", "If yes, describe:", page=11),
        _row("Text Area", "Why were these prevention mechanisms unsuccessful?", page=11),
    ]

    result = dedupe_table_repetitions(rows)

    assert [row.question_text for row in result[-2:]] == [
        "If yes, describe:",
        "Why were these prevention mechanisms unsuccessful?",
    ]
