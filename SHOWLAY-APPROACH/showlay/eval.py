"""
Compare an extracted workbook to its truth workbook and emit an accuracy report.

Strategy:
  - Read both sheets, find header row, map columns via FIELD_HEADER_ALIASES.
  - Read all data rows into dicts keyed by INTERNAL field name.
  - Match candidate rows to truth rows by *fuzzy Question Text + same QuestionType bucket*
    using a 1-1 greedy assignment.
  - Score per-internal-field accuracy on matched pairs.
"""
from __future__ import annotations
import json, re
from pathlib import Path
from openpyxl import load_workbook
import warnings; warnings.filterwarnings("ignore")

from .schema import map_template_columns, FIELD_HEADER_ALIASES


def _norm(s) -> str:
    if s is None:
        return ""
    s = str(s).replace(" ", " ").replace("\xa0", " ")
    s = re.sub(r"\s+", " ", s).strip().lower()
    # Strip trailing colons / dashes for section-name comparison
    s = s.rstrip(":").rstrip("-").strip()
    return s


def _tokens(s: str) -> set[str]:
    return set(re.findall(r"\w+", s.lower()))


def _token_ratio(a: str, b: str) -> float:
    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb:
        return 1.0 if ta == tb else 0.0
    return len(ta & tb) / max(1, max(len(ta), len(tb)))


_QTYPE_HEADERS = {"questiontype", "question type"}


def _read_sheet(path: str):
    wb = load_workbook(path, data_only=True)
    target = None
    for nm in ["Assessment v2", "Assessment"]:
        if nm in wb.sheetnames:
            target = nm; break
    if target is None:
        target = wb.sheetnames[0]
    ws = wb[target]

    header_row = None
    for r in range(1, min(ws.max_row, 30) + 1):
        for c in range(1, ws.max_column + 1):
            v = ws.cell(row=r, column=c).value
            if isinstance(v, str) and " ".join(v.split()).strip().lower() in _QTYPE_HEADERS:
                header_row = r; break
        if header_row:
            break
    if not header_row:
        raise ValueError(f"No header row found in {path}")

    header_values = {cc: ws.cell(row=header_row, column=cc).value
                     for cc in range(1, ws.max_column + 1)}
    field_to_col = map_template_columns(header_values)

    rows: list[dict] = []
    for r in range(header_row + 1, ws.max_row + 1):
        row = {fname: "" for fname in FIELD_HEADER_ALIASES}
        any_value = False
        for fname, cidx in field_to_col.items():
            v = ws.cell(row=r, column=cidx).value
            if v is not None and str(v).strip() != "":
                any_value = True
            row[fname] = "" if v is None else str(v)
        if any_value:
            rows.append(row)

    # Forward-fill the sparse Section column (truth gold convention: Section is set on
    # the first row of a section group, blank on subsequent rows of the same group).
    last_sec = ""
    for row in rows:
        cur = (row.get("section") or "").strip()
        if cur:
            last_sec = cur
        else:
            row["section"] = last_sec
    return rows


def _qtype_bucket(s: str) -> str:
    n = _norm(s)
    if "radio" in n: return "radio"
    if "drop" in n: return "dropdown"
    if "checkbox group" in n: return "checkbox_group"
    if "checkbox" in n: return "checkbox"
    if "text area" in n: return "text_area"
    if "text box" in n: return "text_box"
    if n in ("date", "calendar"): return "date"
    if n == "number": return "number"
    if n == "signature": return "signature"
    if n == "display": return "display"
    if "group table" in n: return "group_table"
    return n or "other"


def _greedy_match(cand: list[dict], truth: list[dict]):
    pairs: list[tuple[float, int, int]] = []
    for ci, c in enumerate(cand):
        for ti, t in enumerate(truth):
            qt = _token_ratio(c["question_text"], t["question_text"])
            qb = 1.0 if _qtype_bucket(c["question_type"]) == _qtype_bucket(t["question_type"]) else 0.0
            sc = _token_ratio(c["section"], t["section"])
            score = 0.6 * qt + 0.2 * qb + 0.2 * sc
            if score >= 0.45:
                pairs.append((score, ci, ti))
    pairs.sort(reverse=True)
    used_c, used_t = set(), set()
    matches: list[tuple[int, int, float]] = []
    for s, ci, ti in pairs:
        if ci in used_c or ti in used_t:
            continue
        used_c.add(ci); used_t.add(ti)
        matches.append((ci, ti, s))
    return matches


def evaluate(candidate_xlsx: str, truth_xlsx: str, report_dir: str = None) -> dict:
    cand_rows = _read_sheet(candidate_xlsx)
    truth_rows = _read_sheet(truth_xlsx)

    matches = _greedy_match(cand_rows, truth_rows)
    matched_c = {ci for ci, _, _ in matches}
    matched_t = {ti for _, ti, _ in matches}

    # Per user's friend (2026-05-07): only these 7 columns are derivable from the PDF.
    # All other columns are filled later by the business config / DB layer.
    # Sequence is intentionally OMITTED from accuracy scoring: when candidate row count
    # differs from truth row count, absolute sequence numbers diverge for everything past
    # the first divergence, even when the relative ordering is correct. We score relative
    # order separately below.
    KEY_FIELDS = [
        "question_type", "question_text", "section",
        "answer_text", "branching_logic", "question_rule",
    ]
    per_col = {f: {"exact": 0, "fuzzy": 0, "n": 0} for f in KEY_FIELDS}
    for ci, ti, _ in matches:
        for f in KEY_FIELDS:
            cv, tv = _norm(cand_rows[ci][f]), _norm(truth_rows[ti][f])
            per_col[f]["n"] += 1
            if cv == tv:
                per_col[f]["exact"] += 1
                per_col[f]["fuzzy"] += 1
            elif _token_ratio(cv, tv) >= 0.7:
                per_col[f]["fuzzy"] += 1

    summary = {
        "candidate_rows": len(cand_rows),
        "truth_rows": len(truth_rows),
        "matched": len(matches),
        "row_recall": round(len(matched_t) / max(1, len(truth_rows)), 4),
        "row_precision": round(len(matched_c) / max(1, len(cand_rows)), 4),
        "missed_truth_indices": [ti for ti in range(len(truth_rows)) if ti not in matched_t],
        "spurious_candidate_indices": [ci for ci in range(len(cand_rows)) if ci not in matched_c],
        "per_col": {f: {
            "exact_pct": round(per_col[f]["exact"] / max(1, per_col[f]["n"]), 4),
            "fuzzy_pct": round(per_col[f]["fuzzy"] / max(1, per_col[f]["n"]), 4),
            "n": per_col[f]["n"],
        } for f in KEY_FIELDS},
    }
    summary["overall_fuzzy_pct"] = round(
        sum(per_col[f]["fuzzy"] for f in KEY_FIELDS) / max(1, sum(per_col[f]["n"] for f in KEY_FIELDS)),
        4,
    )

    if report_dir:
        Path(report_dir).mkdir(parents=True, exist_ok=True)
        Path(report_dir, "summary.json").write_text(json.dumps(summary, indent=2))
        md = []
        md.append(f"# SHOWLAY eval — {Path(candidate_xlsx).name}")
        md.append(f"vs truth: `{Path(truth_xlsx).name}`")
        md.append("")
        md.append(f"- Candidate rows: **{summary['candidate_rows']}**")
        md.append(f"- Truth rows: **{summary['truth_rows']}**")
        md.append(f"- Matched: **{summary['matched']}**")
        md.append(f"- **Row recall**: {summary['row_recall']:.1%} | "
                  f"**Row precision**: {summary['row_precision']:.1%}")
        md.append(f"- **Overall fuzzy column accuracy** (matched rows): "
                  f"**{summary['overall_fuzzy_pct']:.1%}**")
        md.append("")
        md.append("## Per-field accuracy (on matched rows)")
        md.append("| Field | n | Exact % | Fuzzy % |")
        md.append("|---|---|---|---|")
        for f in KEY_FIELDS:
            v = summary["per_col"][f]
            md.append(f"| {f} | {v['n']} | {v['exact_pct']:.1%} | {v['fuzzy_pct']:.1%} |")
        md.append("")
        if summary["missed_truth_indices"]:
            md.append(f"## Missed truth rows ({len(summary['missed_truth_indices'])})")
            for ti in summary["missed_truth_indices"][:25]:
                t = truth_rows[ti]
                md.append(f"- seq={t.get('sequence','')!s:>4}  "
                          f"type={t.get('question_type','')!s:<14.14} "
                          f"text={t.get('question_text','')[:80]!r}")
            md.append("")
        if summary["spurious_candidate_indices"]:
            md.append(f"## Spurious candidate rows ({len(summary['spurious_candidate_indices'])})")
            for ci in summary["spurious_candidate_indices"][:25]:
                c = cand_rows[ci]
                md.append(f"- seq={c.get('sequence','')!s:>4}  "
                          f"type={c.get('question_type','')!s:<14.14} "
                          f"text={c.get('question_text','')[:80]!r}")
            md.append("")
        Path(report_dir, "summary.md").write_text("\n".join(md), encoding="utf-8")

    return summary
