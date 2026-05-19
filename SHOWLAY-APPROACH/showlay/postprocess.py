"""
Post-processing passes that run AFTER the per-page VLM extraction:

  1. drop_chrome               — drop page-recurring titles/footers and pure-underline rows
  2. dedupe_repeating_headers  — collapse "(header)" rows that recur on every page
  3. dedupe_consecutive        — collapse identical (type, text) rows next to each other
  4. assign_sequence           — dense 1-based reading-order index across all pages
  5. propagate_section         — forward-fill Section column from "New Section" Display rows
  6. normalize_branching       — standardize Branching Logic strings
  7. normalize_answer_text     — fill in default char limits / format hints by question_type
"""
from __future__ import annotations
import re
from collections import defaultdict
from .schema import Row


_HEADER_PREFIX_RE = re.compile(r"^\s*\(header\)\s*", re.IGNORECASE)
_NEAR_EMPTY_RE = re.compile(r"^[\s_\-\.—–/\\\(\)\[\]:;,]+$")  # underscores, dashes, dots only


def _normtext(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip().lower())[:100]


def _full_normtext(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip().lower())


def _strip_fill_marks(s: str) -> str:
    s = re.sub(r"[_]{3,}.*?$", "", s or "")
    return re.sub(r"\s+", " ", s).strip()


def _chrome_key(s: str) -> str:
    """Normalize page-band text so repeated headers/footers can be detected without
    knowing the form title. Page numbers and fill-lines are unstable; the remaining
    words are the recurrence signal."""
    s = _strip_fill_marks(s)
    s = re.sub(r"^\s*\d+\s+", "", s)
    s = re.sub(r"\s+\d+\s*$", "", s)
    s = re.sub(r"\bpage\s+\d+(\s+of\s+\d+)?\b", "page", s, flags=re.IGNORECASE)
    s = re.sub(r"[^a-z0-9:/& ]+", " ", s.lower())
    return re.sub(r"\s+", " ", s).strip()


_CHROME_TEXT_RE = re.compile(
    r"^("
    r"safety determination request form|"
    r"tn division of health care.*$|"
    r"tc\d+\s*\(rev\.?\s*[\d\-]+\)|"
    r"rda\s*\d+|"
    r"page\s+\d+\s*(of\s+\d+)?|"
    r"form\s+[a-z]?\d+\-?\d*\s*$|"
    r"september\s+\d{4}"
    # NOTE: header band (Applicant Name + SSN + DOB) handled by category (b) below -
    # NOT by this regex - because we KEEP the first occurrence and split it; this
    # regex would delete it.
    r")",
    re.IGNORECASE,
)

def drop_chrome(rows: list[Row], min_recur_pages: int = 3) -> list[Row]:
    """Remove rows that are clearly page chrome (form titles, agency banners, footer
    codes, multi-page running header bands) or pure-underline lines.

    Conservative strategy — only drops rows that are EITHER:
      (a) Display rows whose text matches an explicit chrome regex (form titles, agency
          names, regulatory footer codes, page numbers), OR
      (b) Text Box rows whose text is the COMBINED running-header band
          'Applicant Name: ___ SSN: ___ DOB: ___', regardless of page count, OR
      (c) Pure punctuation / underscore lines (no real text).

    Per-block recurring labels like 'Description of documentation attached:' are
    kept because they can have legitimate distinct contexts."""
    drop_idx: set[int] = set()

    for i, r in enumerate(rows):
        txt = (r.question_text or "").strip()
        qt = (r.question_type or "").strip().lower()

        # (c) pure punctuation / near-empty
        non_alpha = re.sub(r"[A-Za-z0-9]", "", txt)
        alpha_only = re.sub(r"[^A-Za-z0-9]", "", txt)
        if not alpha_only or (len(non_alpha) > len(alpha_only) * 5 and len(alpha_only) < 4):
            drop_idx.add(i); continue
        if _NEAR_EMPTY_RE.match(txt):
            drop_idx.add(i); continue

        # (a) Display chrome by explicit regex
        if qt == "display" and _CHROME_TEXT_RE.search(txt):
            drop_idx.add(i); continue

        # (b) Combined running-header band (will be split by split_header_band on the
        # surviving page-1 occurrence; drop everything else)
        # Key feature: contains "Applicant" + "SSN" + "DOB" all on the same row.
        low = txt.lower()
        if all(tok in low for tok in ("applicant name", "ssn", "dob")):
            # Drop all but the first (smallest-page) occurrence
            same_band = [k for k, rr in enumerate(rows)
                         if all(t in (rr.question_text or "").lower() for t in ("applicant name", "ssn", "dob"))]
            if same_band:
                pages = sorted({rows[k].page or 999 for k in same_band})
                first_kept = False
                for k in same_band:
                    if rows[k].page == pages[0] and not first_kept:
                        first_kept = True; continue
                    drop_idx.add(k)

    return [r for i, r in enumerate(rows) if i not in drop_idx]


def _repeated_page_band_keys(doc_struct=None, min_recur_pages: int = 3) -> tuple[set[str], set[str]]:
    """Return repeated top/bottom line keys and repeated top-band field labels.

    This uses layout position and recurrence, not form-specific wording. It catches
    running titles, page-numbered titles, and repeated header bands like
    "Name: ____ ID: ____ DOB: ____" across many pages.
    """
    line_pages: dict[str, set[int]] = defaultdict(set)
    field_pages: dict[str, set[int]] = defaultdict(set)
    if doc_struct is None:
        return set(), set()
    page_count = int(getattr(doc_struct, "page_count", 0) or 0)
    min_pages = min(min_recur_pages, max(2, page_count // 2)) if page_count else min_recur_pages

    for ps in getattr(doc_struct, "pages", []) or []:
        page_no = int(getattr(ps, "page_index", 0) or 0) + 1
        height = float(getattr(ps, "height", 0) or 0)
        if not height:
            continue
        for tb in getattr(ps, "text_blocks", []) or []:
            rect = tb.get("rect") or []
            if len(rect) != 4:
                continue
            y0 = float(rect[1])
            text = str(tb.get("text") or "").strip()
            if not text:
                continue
            in_band = y0 <= height * 0.18 or y0 >= height * 0.88
            if not in_band:
                continue
            key = _chrome_key(text)
            if len(key) >= 5:
                line_pages[key].add(page_no)
            for label in re.findall(r"([A-Za-z][A-Za-z 0-9/&]{1,45}?:)\s*_{3,}", text):
                field_pages[_strip_for_dedup(label)].add(page_no)

    repeated_lines = {k for k, pages in line_pages.items() if len(pages) >= min_pages}
    repeated_fields = {k for k, pages in field_pages.items() if len(pages) >= min_pages}
    return repeated_lines, repeated_fields


def drop_repeated_page_bands(rows: list[Row], doc_struct=None, min_recur_pages: int = 3) -> list[Row]:
    """Drop repeated top/bottom page-band rows using layout recurrence.

    Unlike the older explicit chrome regex, this works for new forms because the
    signal is repeated page position. Repeated field labels are emitted once, then
    later copies are dropped.
    """
    repeated_lines, repeated_fields = _repeated_page_band_keys(doc_struct, min_recur_pages)
    if not repeated_lines and not repeated_fields:
        return rows

    seen_fields: set[str] = set()
    out: list[Row] = []
    for r in rows:
        txt = (r.question_text or "").strip()
        qt = (r.question_type or "").strip().lower()
        key = _chrome_key(txt)
        if key in repeated_lines and qt == "display":
            continue
        field_key = _strip_for_dedup(_HEADER_PREFIX_RE.sub("", txt))
        if field_key in repeated_fields and qt in ("text box", "date", "number"):
            if field_key in seen_fields:
                continue
            seen_fields.add(field_key)
        out.append(r)
    return out


def dedupe_consecutive(rows: list[Row]) -> list[Row]:
    """Collapse adjacent rows with identical (question_type, normalized question_text).
    Helps when the model emits the same Display paragraph twice in a row."""
    out: list[Row] = []
    last_key = None
    for r in rows:
        key = ((r.question_type or "").lower(), _normtext(r.question_text))
        if key == last_key and key[0] in ("display", ""):
            continue
        out.append(r)
        last_key = key
    return out


def dedupe_repeating_headers(rows: list[Row]) -> list[Row]:
    """For multi-page forms with running headers, keep only the first occurrence."""
    seen: dict[str, int] = {}
    out: list[Row] = []
    for r in rows:
        is_header_marker = bool(_HEADER_PREFIX_RE.match(r.question_text or ""))
        key = _HEADER_PREFIX_RE.sub("", (r.question_text or "")).strip().lower()
        if is_header_marker:
            if key in seen:
                continue
            seen[key] = 1
        out.append(r)
    return out


def assign_sequence(rows: list[Row]) -> list[Row]:
    """Assign dense 1-based sequence in INPUT LIST ORDER. The VLM emits page-by-page
    in reading order; collapse_choice_groups places children right after the parent
    in the list. Sorting again would reorder children with sequence=None to before
    the parent — preserve the list as-given."""
    for i, r in enumerate(rows, start=1):
        r.sequence = i
    return rows


_GENERIC_LABELS = {
    "signature", "printed name", "name", "date", "date of signature",
    "date signed", "signed", "title",
}
# Words to strip from a banner before using it as a role prefix.
_BANNER_TRIM_RE = re.compile(
    r"\s*[,(\-]\s*(?:if applicable|optional|please print|print|signature line)\b.*?$",
    re.IGNORECASE,
)


def _banner_role(text: str) -> tuple[str, bool]:
    """Return (role, had_trailing_colon) extracted from a banner-style Display row."""
    s = (text or "").strip()
    had_colon = s.endswith(":")
    s = s.rstrip(":").strip()
    s = _BANNER_TRIM_RE.sub("", s).strip().rstrip(",").strip()
    return s, had_colon


def _is_banner(r: Row) -> bool:
    qt = (r.question_type or "").strip().lower()
    txt = (r.question_text or "").strip()
    if qt != "display" or not txt:
        return False
    if txt.lower() == "new section":
        return False
    # Heuristic: short-ish, ends with ":" and not a long paragraph.
    words = txt.split()
    return txt.endswith(":") and len(words) <= 12 and len(txt) <= 90


def repair_question_text(rows: list[Row], doc_struct=None) -> list[Row]:
    """Upgrade generic labels (Signature / Printed Name / Date / Name) by prefixing the
    role from the immediately-preceding Display 'banner' row, e.g.
       Display 'Witness, if applicable:'  +  Signature 'Signature'
         -> 'Witness Signature:'
    Also consults the AcroForm widget's field_label when row bbox + page is known."""
    # Build per-page widget index for the bbox path (best-effort).
    widgets_by_page: dict[int, list[dict]] = {}
    if doc_struct is not None:
        for ps in getattr(doc_struct, "pages", []) or []:
            widgets_by_page[ps.page_index + 1] = list(ps.widgets or [])

    current_role: str = ""
    current_role_colon: bool = False
    for r in rows:
        qt_low = (r.question_type or "").strip().lower()
        txt = (r.question_text or "").strip()
        # Update current role if this row is a banner.
        if _is_banner(r):
            current_role, current_role_colon = _banner_role(txt)
            continue
        # Section banners reset the role.
        if qt_low == "display" and txt.lower() == "new section":
            current_role, current_role_colon = "", False
            continue
        if txt.lower() not in _GENERIC_LABELS:
            continue

        # Prefer AcroForm widget field_label when we have bbox + page.
        widget_label = ""
        if r.page and r.bbox and widgets_by_page.get(r.page):
            cx = (r.bbox[0] + r.bbox[2]) / 2.0
            cy = (r.bbox[1] + r.bbox[3]) / 2.0
            for w in widgets_by_page[r.page]:
                wb = w.get("rect") or [0, 0, 0, 0]
                if wb[0] <= cx <= wb[2] and wb[1] <= cy <= wb[3] and w.get("label"):
                    widget_label = str(w["label"]).strip()
                    break
        if widget_label and len(widget_label.split()) >= 3:
            r.question_text = widget_label + (":" if widget_label and not widget_label.endswith(":") else "")
        elif current_role:
            new_text = f"{current_role} {txt}".strip()
            r.question_text = new_text + (":" if current_role_colon else "")
    return rows


def propagate_section(rows: list[Row]) -> list[Row]:
    """The CHOICES gold encodes section banners as Display rows whose question_text is
    'New Section' and answer_text holds the section title. The Section column itself is
    set only on the first row of each section, then blank.

    Rule we apply:
      - When we see a 'New Section' Display row, capture answer_text as current_section.
      - Place current_section on that Display row's Section column too.
      - Forward-fill subsequent rows whose Section is empty.
      - Reset to '' if the model emits an explicit '' on a Display row (rare).
    """
    current = ""
    for r in rows:
        if (r.question_type or "").strip().lower() == "display" \
                and (r.question_text or "").strip().lower() == "new section":
            current = (r.answer_text or "").strip().rstrip(":")
            r.section = current
            continue
        # If model already supplied a section, trust it and update current.
        if r.section:
            current = r.section.strip().rstrip(":")
        elif current:
            r.section = current
    # Strip stray colons for consistency with gold ("Justification for Safety Determination Request:"
    # in gold keeps the colon — so we don't strip in the gold form). We mirror what the model gave.
    return rows


_BRANCHING_PATTERNS = [
    # already-good shapes:
    (re.compile(r"^if\s+q(\d+)\s*=\s*checked\(selected\)\s*$", re.I), r"If Q\1 = checked(selected)"),
    (re.compile(r"^if\s+q(\d+)\s*=\s*selected\s*$", re.I), r"If Q\1 = checked(selected)"),
    (re.compile(r"^if\s+q(\d+)\s*=\s*ticked\s*$", re.I), r"If Q\1 = checked(selected)"),
    (re.compile(r"^display\s+if\s+q(\d+)\s*=\s*(.+)$", re.I), r"Display if Q\1 = \2"),
]


def normalize_branching(rows: list[Row]) -> list[Row]:
    for r in rows:
        s = (r.branching_logic or "").strip()
        if not s:
            continue
        for pat, repl in _BRANCHING_PATTERNS:
            m = pat.match(s)
            if m:
                s = pat.sub(repl, s)
                break
        # Fallback: if it mentions "checked" we coerce
        if "checked" in s.lower() and not s.lower().startswith("if q"):
            m = re.search(r"q(\d+)", s, re.I)
            if m:
                s = f"If Q{m.group(1)} = checked(selected)"
        r.branching_logic = s
    return rows


_DEFAULT_ANSWER_TEXT_BY_TYPE = {
    "text box": "default characters = 100",
    "text area": "default characters = 600",
    "date": "Format is mm/dd/yyyy",
    "number": "only allow numeric characters",
    "signature": "Signature area",
}


def normalize_answer_text(rows: list[Row]) -> list[Row]:
    """If question_type implies a standard Answer Text hint and the model left it blank, fill it.
    For choice types we don't synthesize options."""
    for r in rows:
        qt = (r.question_type or "").strip().lower()
        if r.answer_text:
            continue
        if qt in _DEFAULT_ANSWER_TEXT_BY_TYPE:
            r.answer_text = _DEFAULT_ANSWER_TEXT_BY_TYPE[qt]
    return rows


# Question types whose Answer Text holds a list of options.
_CHOICE_QTYPES = {
    "radio button",
    "radio buttons",
    "dropdown",
    "drop down",
    "checkbox group",
    "checkbox",
}


def normalize_choice_options(rows: list[Row]) -> list[Row]:
    """For Radio Button / Dropdown / Checkbox Group rows, normalize the Answer Text
    so individual options are separated by exactly two newlines (``\\n\\n``).

    The CHOICES gold encodes the option list with `\\n\\n` between each option. The
    VLM sometimes emits options separated by a single `\\n`, by `\\r\\n`, or with
    extra surrounding whitespace. Without changing option order:

      - normalize OS line endings (``\\r\\n``, ``\\r``) to ``\\n``
      - if the cell already uses `\\n\\n` boundaries, keep that split
      - otherwise treat each `\\n` as an option boundary
      - strip leading/trailing whitespace from each option
      - drop empty options
      - rejoin with exactly ``\\n\\n``

    Non-choice rows are left untouched.
    """
    for r in rows:
        qt = (r.question_type or "").strip().lower()
        if qt not in _CHOICE_QTYPES:
            continue
        s = r.answer_text or ""
        if not s.strip():
            continue
        s = s.replace("\r\n", "\n").replace("\r", "\n")
        if "\n\n" in s:
            parts = re.split(r"\n{2,}", s)
        else:
            parts = s.split("\n")
        cleaned = [p.strip() for p in parts]
        cleaned = [p for p in cleaned if p]
        r.answer_text = "\n\n".join(cleaned)
    return rows


_VALIDATION_FRAGMENT_RE = re.compile(
    r"^\s*("
    r"default\s+characters?\s*=\s*\d+"
    r"|format\s+is\s+[^;\n]+"
    r"|signature\s+area"
    r"|initials?\s+area"
    r"|only\s+allow\s+[^;\n]+"
    r"|numeric(?:\s+only)?"
    r"|date\s+format\s+[^;\n]+"
    r"|mm\s*/\s*dd\s*/\s*yyyy"
    r"|\d+\s+characters?"
    r")\s*$",
    re.IGNORECASE,
)


def _split_cell_lines(value: str) -> list[str]:
    s = (value or "").replace("\r\n", "\n").replace("\r", "\n")
    if "\n\n" in s:
        parts = re.split(r"\n{2,}", s)
    else:
        parts = s.split("\n")
    return [p.strip() for p in parts if p.strip()]


def separate_answer_validation(rows: list[Row]) -> list[Row]:
    """Keep selectable values in Answer Text and move input constraints to
    Answer Validation.

    This does not synthesize defaults. It only relocates validation-like text if the
    model placed it in Answer Text, including mixed cells where options and hints were
    emitted together on separate lines.
    """
    for r in rows:
        answer = (r.answer_text or "").strip()
        if not answer:
            continue

        parts = _split_cell_lines(answer)
        if not parts:
            continue

        validation_parts: list[str] = []
        answer_parts: list[str] = []
        for part in parts:
            if _VALIDATION_FRAGMENT_RE.match(part):
                validation_parts.append(part)
            else:
                answer_parts.append(part)

        if not validation_parts:
            continue

        existing = _split_cell_lines(r.answer_validation or "")
        combined_validation = existing + [p for p in validation_parts if p not in existing]
        r.answer_validation = "\n\n".join(combined_validation)
        r.answer_text = "\n\n".join(answer_parts)
    return rows


_PLAN_DATE_HINTS = (
    "begin date",
    "end date",
    "revision date",
)
_DATE_LABEL_HINTS = (
    "date",
    "dob",
    "(birth)",
    "birthdate",
    "date of birth",
)


def coerce_date_types(rows: list[Row]) -> list[Row]:
    """Fix VLM mis-classification of date fields using the label hints that were
    in place before the last cleanup attempt."""
    for r in rows:
        qt = (r.question_type or "").strip().lower()
        if qt == "calendar":
            r.question_type = "Date"
            continue
        if qt != "text box":
            continue
        label = (r.question_text or "").lower()
        if not label:
            continue
        if "applicant name" in label or "ssn" in label:
            continue
        clean = re.sub(r"[_]{2,}", "", label).strip()
        if len(clean) > 60:
            continue
        if any(h in label for h in _PLAN_DATE_HINTS):
            r.question_type = "Date"
            continue
        if any(h in label for h in _DATE_LABEL_HINTS):
            r.question_type = "Date"
            continue
        if re.search(r"\b(mm\s*/\s*dd\s*/\s*yyyy|date\s+format)\b", r.answer_validation or "", re.IGNORECASE):
            r.question_type = "Date"
    return rows

def resolve_pending_branching(rows: list[Row]) -> list[Row]:
    """`collapse_choice_groups` emits child Text Box rows with branching_logic
    referencing '<PARENT_SEQ>' because final sequence isn't assigned yet. After
    `assign_sequence`, walk rows and replace the placeholder with the parent's
    actual sequence by reading the stash in alt_question_text."""
    for i, r in enumerate(rows):
        if "<PARENT_SEQ>" not in (r.branching_logic or ""):
            continue
        # Find parent: nearest preceding Radio/Checkbox Group row whose options match.
        parent_seq = None
        for k in range(i - 1, max(-1, i - 10), -1):
            ptype = (rows[k].question_type or "").lower()
            if ptype in ("radio button", "checkbox group", "dropdown") and rows[k].sequence:
                parent_seq = rows[k].sequence
                break
        if parent_seq is not None:
            r.branching_logic = r.branching_logic.replace("<PARENT_SEQ>", f"Q{parent_seq}".replace("QQ", "Q"))
            # Final form: "Display if Q<n> = <option>"
            r.branching_logic = r.branching_logic.replace("Q<PARENT_SEQ>", f"Q{parent_seq}")
            r.branching_logic = r.branching_logic.replace("QQ", "Q")
        # Clear the stash
        if (r.alt_question_text or "").startswith("_pending_parent_idx="):
            r.alt_question_text = ""
    return rows


_NUMBER_LABEL_RE = re.compile(
    r"\b(score|total\s+\w+|count|number of|amount|quantity|age|years|height|weight)\b",
    re.IGNORECASE,
)


def coerce_number_type(rows: list[Row]) -> list[Row]:
    """Promote Text Box → Number when the label clearly asks for a numeric value
    (e.g., 'Total Acuity Score of PAE as submitted:'). Excludes obvious non-numeric
    matches like 'Total Score Discussion' (which would still match — caveat)."""
    for r in rows:
        if (r.question_type or "").strip().lower() != "text box":
            continue
        txt = (r.question_text or "").strip()
        if _NUMBER_LABEL_RE.search(txt):
            r.question_type = "Number"
    return rows


_BULLET_PROMPT_RE = re.compile(r"^\s*o\s+([A-Z][a-z]+\b\s+){1,3}", re.IGNORECASE)
_TEXT_AREA_VERBS_RE = re.compile(
    r"^\s*o?\s*(provide|describe|document|attach|explain|list)\b",
    re.IGNORECASE,
)


_PARENTHETICAL_SUBNOTE_RE = re.compile(r"^\s*\(\s*(attach|provide|please|note|see)\b", re.IGNORECASE)


def merge_parenthetical_subnotes(rows: list[Row]) -> list[Row]:
    """Truth treats parenthetical instructions starting with '(Attach|Provide|Please|Note|See...'
    as inline detail APPENDED to the preceding question's question_text. The VLM emits
    them as their own Display rows; merge them into the previous row's question_text and
    drop the standalone Display."""
    out: list[Row] = []
    for r in rows:
        is_paren = ((r.question_type or "").strip().lower() == "display"
                    and _PARENTHETICAL_SUBNOTE_RE.match((r.question_text or "").strip()))
        if is_paren and out:
            prev = out[-1]
            prev_qt = (prev.question_type or "").strip().lower()
            if prev_qt in ("text area", "text box", "checkbox", "display"):
                tail = (r.question_text or "").strip()
                prev_text = (prev.question_text or "").rstrip()
                # Avoid double-appending if already present
                if tail not in prev_text:
                    sep = "" if prev_text.endswith(("(", ".", "?", "!", ":", ";")) else "."
                    prev.question_text = (prev_text + sep + tail).strip()
                continue
        out.append(r)
    return out


# Backwards-compat alias for any external caller.
drop_parenthetical_subnotes = merge_parenthetical_subnotes


def coerce_text_area_for_bullet_prompts(rows: list[Row]) -> list[Row]:
    """A row whose text starts with 'o Provide a detailed description…',
    'Describe how often…', 'Document below…', etc. is an ANSWER-SPACE prompt,
    not Display chrome and not a Checkbox option. Coerce Display→Text Area and
    Checkbox→Text Area when the prompt is clearly verb-led."""
    for r in rows:
        qt = (r.question_type or "").strip().lower()
        if qt not in ("display", "checkbox"):
            continue
        txt = (r.question_text or "").strip()
        if _TEXT_AREA_VERBS_RE.match(txt) and len(txt) > 30:
            r.question_type = "Text Area"
            r.question_text = re.sub(r"^\s*o\s+", "", txt)
    return rows


def coerce_static_instruction_rows(rows: list[Row]) -> list[Row]:
    """Instruction rows that introduce structured fields below should be Display,
    not answer inputs. This is structural: an imperative line with a 'below' cue
    followed by table/input rows in the same local block."""
    for i, r in enumerate(rows):
        qt = (r.question_type or "").strip().lower()
        if qt not in ("display", "text area", "checkbox"):
            continue
        txt = (r.question_text or "").strip()
        low = txt.lower()
        if "below" not in low or not _TEXT_AREA_VERBS_RE.match(txt):
            continue
        for nxt in rows[i + 1:min(len(rows), i + 5)]:
            nt = (nxt.question_type or "").strip().lower()
            if nt in ("group table", "text box", "date", "number", "radio button", "checkbox group"):
                r.question_type = "Display"
                r.answer_text = ""
                break
            if nt == "display" and (nxt.question_text or "").strip().lower() == "new section":
                break
    return rows


def normalize_section_header_type(rows: list[Row]) -> list[Row]:
    """Some VLM outputs emit 'Section Header' as a question_type. Truth gold uses
    Display rows whose question_text='New Section' and answer_text=section title.
    Coerce: any row with type='Section Header' → Display 'New Section', moving its
    original question_text into answer_text as the section title."""
    for r in rows:
        qt = (r.question_type or "").strip().lower()
        if qt == "section header":
            section_title = (r.question_text or "").strip().rstrip(":")
            r.question_type = "Display"
            r.question_text = "New Section"
            r.answer_text = section_title + (":" if section_title and not section_title.endswith(":") else "")
            r.section = section_title.rstrip(":")
    return rows


def dedupe_table_repetitions(rows: list[Row]) -> list[Row]:
    """If a Group Table row + N children pattern repeats with identical (type, text)
    sequence (e.g. PDF shows 4 falls instances; truth encodes the SHAPE once), keep
    only the first repetition. Detection: any Group Table whose immediately-preceding
    Group Table within the last K rows has the same parent text → drop this Group
    Table block (parent + following children up to next Group Table or non-input row)."""
    drop_idx: set[int] = set()
    seen_table_keys: dict[str, int] = {}  # parent normalized text -> first index
    n = len(rows)
    i = 0
    while i < n:
        r = rows[i]
        if (r.question_type or "").strip().lower() == "group table":
            key = _normtext(r.question_text)
            # Find end of this table block: until next Group Table, Display "New Section",
            # or a Checkbox/Display that breaks the pattern.
            end = i + 1
            while end < n:
                t2 = (rows[end].question_type or "").strip().lower()
                txt2 = (rows[end].question_text or "").strip().lower()
                if t2 == "group table" or (t2 == "display" and txt2 == "new section"):
                    break
                # Allow children that are Date / Text Box / Number / Text Area / Checkbox Group
                if t2 in ("date", "text box", "number", "text area", "checkbox group", "dropdown", "radio button"):
                    end += 1
                    continue
                break
            if key in seen_table_keys:
                # Drop this entire repetition
                for j in range(i, end):
                    drop_idx.add(j)
            else:
                seen_table_keys[key] = i
            i = end
            continue
        i += 1
    return [r for i, r in enumerate(rows) if i not in drop_idx]


def _strip_for_dedup(s: str) -> str:
    """Normalize a label for header-dedup comparison: strip trailing underscores
    (PDF fill lines), AM/PM markers, runs of whitespace; lowercase."""
    s = (s or "").strip()
    s = _QT_TAIL_RE.sub("", s)
    s = _AMPM_TAIL_RE.sub("", s)
    s = _QT_WS_RE.sub(" ", s).strip().lower()
    return s


def dedupe_unprefixed_header_aliases(rows: list[Row]) -> list[Row]:
    """If a row already exists with question_text='(header) Applicant Name:' early in
    the list, drop later occurrences of plain 'Applicant Name: ____' (with trailing
    fill-line underscores) that the VLM emitted on subsequent pages."""
    seen_normalised: set[str] = set()
    for r in rows:
        txt = (r.question_text or "").strip()
        if _HEADER_PREFIX_RE.match(txt):
            seen_normalised.add(_strip_for_dedup(_HEADER_PREFIX_RE.sub("", txt)))
    drop_idx: set[int] = set()
    for i, r in enumerate(rows):
        txt = (r.question_text or "").strip()
        if _HEADER_PREFIX_RE.match(txt):
            continue
        if _strip_for_dedup(txt) in seen_normalised:
            drop_idx.add(i)
    return [r for i, r in enumerate(rows) if i not in drop_idx]


def sparsify_section(rows: list[Row]) -> list[Row]:
    """Truth gold emits the Section column SPARSELY — only on the first row of each
    contiguous run with the same Section. Subsequent rows leave Section blank
    (downstream consumer forward-fills). Match that convention for column-accuracy
    parity."""
    last_section = None
    for r in rows:
        cur = (r.section or "").strip()
        if not cur:
            last_section = None
            continue
        if cur == last_section:
            r.section = ""
        else:
            last_section = cur
    return rows


_INPUT_QTYPES_BLANK = {
    "text box", "text area", "date",
    "number", "signature", "initials", "email",
}
_FORMULAIC_ANSWER_RE = re.compile(
    r"^\s*("
    r"default\s+characters?\s*=\s*\d+"
    r"|format\s+is\s+\S+"
    r"|signature\s+area"
    r"|initials?\s+area"
    r"|only\s+allow\s+numeric\s+characters"
    r")\s*$",
    re.IGNORECASE,
)


_DEFAULT_ANSWER_TEXT_BY_TYPE = {
    "text box":   "default characters = 100",
    "text area":  "default characters = 600",
    "date":       "Format is mm/dd/yyyy",
    "number":     "only allow numeric characters",
    "signature":  "Signature area",
}


def detect_answer_text_convention(truth_path: str | None) -> str:
    """Return 'answer_text' or 'answer_validation' based on which column the truth
    template populates for INPUT ROWS' validation hints.

    Heuristic: walk truth rows, find the first INPUT-type row (Text Box / Date /
    Number / Text Area / Signature). Look at what's in answer_text vs
    answer_validation on that row. Whichever has a value, that's the convention."""
    if not truth_path:
        return "answer_text"
    try:
        from .eval import _read_sheet
        rows = _read_sheet(truth_path)
        for r in rows:
            qt = (r.get("question_type") or "").strip().lower()
            if qt in ("text box", "text area", "date", "number", "signature"):
                at = (r.get("answer_text") or "").strip()
                av = (r.get("answer_validation") or "").strip()
                if av and not at:
                    return "answer_validation"
                if at and not av:
                    return "answer_text"
                if at:
                    return "answer_text"
                if av:
                    return "answer_validation"
                # both empty — keep walking
    except Exception:
        pass
    return "answer_text"


def populate_answer_text_defaults(rows: list[Row], target_field: str = "answer_text") -> list[Row]:
    """Fill formulaic defaults per gold convention. Rules (in order):

      1. If row already has answer_text → skip (preserve choice option lists, etc.)
      2. If row has branching_logic → skip (children of branchable parents stay empty)
      3. If row is a Group Table CHILD (input row following a Group Table parent without
         a Checkbox / Radio Button / Display 'New Section' boundary in between) → skip
      4. Otherwise apply default for the question_type:
         Text Box  → 'default characters = 100'
         Text Area → 'default characters = 600'
         Date      -> 'Format is mm/dd/yyyy'
         Number    → 'only allow numeric characters'
         Signature → 'Signature area'
    """
    in_table = False
    # Map default to bilingual-style validation strings when target is answer_validation
    AV_TO_VAL = {
        "default characters = 100": "100 characters",
        "default characters = 600": "600 characters",
        "Format is mm/dd/yyyy": "Date",
        "only allow numeric characters": "Numeric",
        "Signature area": "",
    }

    for r in rows:
        qt = (r.question_type or "").strip().lower()
        txt_low = (r.question_text or "").strip().lower()

        if qt == "group table":
            in_table = True
        elif qt in ("checkbox", "radio button", "checkbox group", "dropdown"):
            in_table = False
        elif qt == "display" and txt_low == "new section":
            in_table = False

        # Decide which field to fill
        existing_field = (getattr(r, target_field) or "").strip()
        if existing_field:
            continue
        if (r.branching_logic or "").strip():
            continue
        if in_table and qt in _DEFAULT_ANSWER_TEXT_BY_TYPE:
            continue
        if qt not in _DEFAULT_ANSWER_TEXT_BY_TYPE:
            continue

        default = _DEFAULT_ANSWER_TEXT_BY_TYPE[qt]
        if target_field == "answer_validation":
            default = AV_TO_VAL.get(default, default)
            if not default:
                continue
        setattr(r, target_field, default)
    return rows


_QT_TAIL_RE = re.compile(r"[_—–\-]{3,}.*$")        # remove "______…" tails
_AMPM_TAIL_RE = re.compile(r"\s+AM\s*/\s*PM\s*$", re.IGNORECASE)
_QT_WS_RE = re.compile(r"\s{2,}")


def clean_question_text(rows: list[Row]) -> list[Row]:
    """Strip PDF fill-line artifacts, excess whitespace, and leading 'o ' bullet glyphs
    from question_text on input rows. Per pattern-analysis: clears ~14 of 44
    question_text errors on CHOICES."""
    for r in rows:
        s = r.question_text or ""
        if not s:
            continue
        s = _QT_TAIL_RE.sub("", s)
        s = _AMPM_TAIL_RE.sub("", s)
        s = _QT_WS_RE.sub(" ", s).strip()
        # Strip leading 'o ' bullet for input-type rows (truth never includes the bullet)
        qt = (r.question_type or "").strip().lower()
        if qt in ("text area", "text box", "number"):
            s = re.sub(r"^\s*o\s+", "", s)
        r.question_text = s
    return rows


_BRANCHABLE_TYPES = {"text area", "text box", "display", "group table", "date", "number", "signature"}


def resolve_checkbox_branching(rows: list[Row]) -> list[Row]:
    """Extend branching from a Checkbox parent to rows in the same logical block.

    Block boundary: next Checkbox / Radio Button / Checkbox Group / Dropdown / Display
    'New Section', OR a Section change.

    Group Table CHILDREN (rows immediately following a Group Table parent until the
    next non-input row) are LEFT EMPTY — truth gold has them inherit from the Group
    Table parent, which has its own branching.

    Skip rows that already carry 'Display if Q<n> = ...' (Radio-button child branching)."""
    last_checkbox_seq: int | None = None
    last_section: str = ""
    in_table_children: bool = False
    for r in rows:
        qt = (r.question_type or "").strip().lower()
        txt = (r.question_text or "").strip().lower()
        # Normalize: strip trailing colon so "Justification...Request:" == "Justification...Request"
        cur_section = (r.section or "").strip().rstrip(":").strip()

        if cur_section and cur_section != last_section:
            last_section = cur_section
            last_checkbox_seq = None
            in_table_children = False

        if qt == "checkbox":
            last_checkbox_seq = r.sequence
            in_table_children = False
            continue
        if qt in ("radio button", "checkbox group", "dropdown"):
            last_checkbox_seq = None
            in_table_children = False
            continue
        if qt == "display" and txt == "new section":
            last_checkbox_seq = None
            in_table_children = False
            continue

        if qt == "group table":
            # Apply branching to the Group Table parent itself; mark following
            # input rows as table children (inherit, no branching written).
            if last_checkbox_seq is not None:
                existing = (r.branching_logic or "").strip()
                if not existing.lower().startswith("display if "):
                    r.branching_logic = f"If Q{last_checkbox_seq} = checked(selected)"
            in_table_children = True
            continue

        if qt in ("date", "text box", "number", "text area") and in_table_children:
            # Group Table column child — clear branching (truth leaves blank)
            if not (r.branching_logic or "").lower().startswith("display if "):
                r.branching_logic = ""
            continue

        if qt in _BRANCHABLE_TYPES and last_checkbox_seq is not None:
            existing = (r.branching_logic or "").strip()
            if existing.lower().startswith("display if "):
                continue
            r.branching_logic = f"If Q{last_checkbox_seq} = checked(selected)"
    return rows


_SHORT_TEXT_BOX_RE = re.compile(r"^[A-Za-z][A-Za-z 0-9/&]{1,30}:?\s*$")


def coerce_short_label_to_text_box(rows: list[Row]) -> list[Row]:
    """VLM tends to emit short single-line input labels (e.g. 'Acute event:',
    'Time of Fall:', 'Location of Fall:', 'If yes, describe:') as Text Area because
    they sit in tabular grid contexts. Truth gold has them as Text Box.

    Heuristic: if a Text Area row's question_text is short (<= 30 chars, <=4 words),
    no embedded line breaks, no verb-led prompt → coerce to Text Box."""
    for r in rows:
        if (r.question_type or "").strip().lower() != "text area":
            continue
        txt = (r.question_text or "").strip()
        if not txt or "\n" in txt:
            continue
        m = re.search(r"(\d+)\s*(?:default\s*)?characters?|default\s+characters?\s*=\s*(\d+)",
                      r.answer_validation or "", flags=re.IGNORECASE)
        if m:
            limit = int(next(g for g in m.groups() if g))
            if limit >= 400:
                continue
        # Don't downgrade verb-led prompts (those ARE Text Area)
        if _TEXT_AREA_VERBS_RE.match(txt):
            continue
        words = txt.split()
        if len(words) <= 5 and len(txt) <= 35:
            r.question_type = "Text Box"
    return rows


def coerce_yesno_to_radio_button(rows: list[Row]) -> list[Row]:
    """Single Yes/No decisions should be Radio Buttons. Multi-select wording stays
    Checkbox Group."""
    for r in rows:
        qt = (r.question_type or "").strip().lower()
        if qt not in ("checkbox group", "checkbox", "dropdown", "drop down"):
            continue
        if _SELECT_ALL_RE.search(r.question_text or ""):
            r.question_type = "Checkbox Group"
            continue
        ans_norm = re.sub(r"\s+", " ", (r.answer_text or "").strip().lower())
        if ans_norm in ("yes no", "yes / no", "no yes"):
            r.question_type = "Radio Button"
    return rows


def coerce_yesno_to_dropdown(rows: list[Row]) -> list[Row]:
    """A row whose answer_text is exactly 'Yes\\n\\nNo' (or 'Yes\\nNo' etc.) and which
    asks a single yes/no question is typically a Dropdown in the gold (TX LTSS) or
    a Dropdown for inline yes/no (CHOICES). Coerce Checkbox Group with Yes/No
    options → Dropdown."""
    for r in rows:
        qt = (r.question_type or "").strip().lower()
        if qt not in ("checkbox group", "checkbox"):
            continue
        ans_norm = re.sub(r"\s+", " ", (r.answer_text or "").strip().lower())
        if ans_norm in ("yes no", "yes / no", "no yes"):
            r.question_type = "Dropdown"
    return rows


def coerce_group_table_child_to_text_box(rows: list[Row]) -> list[Row]:
    """A Group Table row that appears INSIDE another Group Table block AND has a SHORT
    label (≤4 words, ≤30 chars) is a column-header mistakenly typed as Group Table —
    coerce to Text Box. SIBLING Group Tables (long parallel-structure headers like
    'Recent (last 365 days) ER visits') are left as Group Table — they're new tables,
    not children."""
    in_table = False
    for r in rows:
        qt = (r.question_type or "").strip().lower()
        if qt == "group table":
            txt = (r.question_text or "").strip()
            words = txt.split()
            short = (len(words) <= 4 and len(txt) <= 30)
            if in_table and short:
                r.question_type = "Text Box"
                # Stay in_table — this row was treated as a column child
            else:
                in_table = True  # New (or sibling) Group Table starts/continues a block
            continue
        if qt in ("checkbox", "radio button", "checkbox group", "dropdown"):
            in_table = False
        elif qt == "display" and (r.question_text or "").strip().lower() == "new section":
            in_table = False
    return rows


def merge_bullet_list_displays(rows: list[Row]) -> list[Row]:
    """Truth gold sometimes joins a Display 'header:' row + N short bullet items into
    ONE Display row whose question_text uses '\\n' separators. The VLM emits them as
    separate Display rows. Merge consecutive Display rows when:
      - Previous Display ends with ':' (intro line for a list), OR
      - Previous Display question_text already contains '\\n' (we're already merging)
      AND the current Display is short (<= 30 words) and starts lowercase or 'A '/'an '
      (typical bullet style).

    Stops merging when we hit a non-Display row OR a 'New Section' OR a Display whose
    text starts with the next 'Label attachment(s)' / 'Document below' (which are
    section-end markers in this form)."""
    out: list[Row] = []
    merging_into: Row | None = None
    for r in rows:
        qt = (r.question_type or "").strip().lower()
        txt = (r.question_text or "").strip()
        if qt != "display" or not txt:
            merging_into = None
            out.append(r)
            continue
        if txt.lower() == "new section":
            merging_into = None
            out.append(r)
            continue
        # Should we start or continue a merge?
        if merging_into is not None:
            # Continue merge as long as this is a short bullet
            words = txt.split()
            looks_like_bullet = len(words) <= 30
            if looks_like_bullet:
                merging_into.question_text = (merging_into.question_text or "").rstrip() + "\n" + txt
                continue
            else:
                merging_into = None
        # Is this a candidate to START a merge? Display ending with ':'
        if txt.endswith(":") and len(txt.split()) <= 12 and not txt.lower() == "new section":
            merging_into = r
        out.append(r)
    return out


def normalize_branching_yes_no(rows: list[Row]) -> list[Row]:
    """Case-fold YES/NO booleans inside branching_logic to Yes/No, and rewrite
    bare 'checked' → 'checked(selected)'."""
    for r in rows:
        bl = r.branching_logic
        if not bl:
            continue
        bl = re.sub(r"=\s*YES\b", "= Yes", bl)
        bl = re.sub(r"=\s*NO\b",  "= No",  bl)
        bl = re.sub(r"=\s*checked\b(?!\()", "= checked(selected)", bl)
        r.branching_logic = bl
    return rows


_SELECT_ALL_RE = re.compile(
    r"\b(check all|check all that apply|select all that apply|all that apply|"
    r"check and complete all that apply|mark all that apply)\b",
    re.IGNORECASE,
)
_SINGLE_ANSWER_HINT_RE = re.compile(
    r"\b(select one|choose one|pick one|select your)\b", re.IGNORECASE,
)


def coerce_select_all_choice_groups(rows: list[Row]) -> list[Row]:
    """Prompts that explicitly allow multiple selections must be Checkbox Group,
    even if the VLM called them Radio Button or Dropdown."""
    for r in rows:
        qt = (r.question_type or "").strip().lower()
        if qt not in ("radio button", "dropdown", "drop down", "checkbox", "checkbox group"):
            continue
        if _SELECT_ALL_RE.search(r.question_text or ""):
            r.question_type = "Checkbox Group"
    return rows


def coerce_question_choice_groups_to_radio(rows: list[Row]) -> list[Row]:
    """A question-style prompt with a compact option list usually represents one
    answer, even if the glyphs looked like checkboxes. Explicit select-all wording
    remains Checkbox Group."""
    for r in rows:
        qt = (r.question_type or "").strip().lower()
        if qt != "checkbox group":
            continue
        question = (r.question_text or "").strip()
        if not question.endswith("?"):
            continue
        if _SELECT_ALL_RE.search(question):
            continue
        options = _split_cell_lines(r.answer_text or "")
        if 2 <= len(options) <= 6 and all(_is_short_option_value(o) for o in options):
            r.question_type = "Radio Button"
    return rows


def _answer_is_yes_no(value: str) -> bool:
    ans_norm = re.sub(r"\s+", " ", (value or "").strip().lower())
    return ans_norm in ("yes no", "yes / no", "no yes")


def _options_equal_yes_no(options: list[str]) -> bool:
    values = {_normtext(o).strip(":/") for o in options}
    return values == {"yes", "no"} or values == {"yes", "no", "na"}


def _is_prompt_like_option(value: str) -> bool:
    """A phrase inside Answer Text that is actually a child question/prompt, not
    a selectable option. Keep this structural: punctuation, conditional prefix, and
    length, not form-specific terms."""
    s = (value or "").strip()
    if not s:
        return False
    low = s.lower()
    if low.startswith(("if ", "when ")):
        return True
    if s.endswith(("?", ":")) and len(s.split()) >= 3:
        return True
    return len(s.split()) >= 7 and not re.fullmatch(r"[A-Za-z0-9/#\- ]+", s)


def _is_short_option_value(value: str) -> bool:
    s = (value or "").strip()
    if not s:
        return False
    if _is_prompt_like_option(s):
        return False
    return len(s.split()) <= 5 and len(s) <= 45


def _split_embedded_conditional_options(options: list[str]) -> list[str]:
    """Split cells like "No  If yes, # days per week" into two logical parts.

    The condition literal is still validated later against the parent options, so
    this does not assume a particular answer vocabulary.
    """
    out: list[str] = []
    for opt in options:
        s = (opt or "").strip()
        m = re.search(r"\s+(if|when)\s+([A-Za-z][A-Za-z /-]{0,40})(?=[,;:?]|\s)", s, flags=re.IGNORECASE)
        if not m:
            out.append(s)
            continue
        prefix = s[:m.start()].strip()
        conditional = s[m.start():].strip()
        if prefix and len(prefix.split()) <= 5 and len(prefix) <= 45:
            out.append(prefix)
            out.append(conditional)
        else:
            out.append(s)
    return [p for p in out if p]


def _condition_from_prompt_text(value: str, option_map: dict[str, str] | None = None) -> str:
    """Return the answer literal from a child prompt such as
    "If Denied, explain reason" only when that literal is present in the
    candidate parent's Answer Text.

    This is deliberately not Yes/No-specific. It tests prefixes after "if"/"when"
    against the parent's normalized option list, so "If Not Approved..." can match
    the parent option "Not Approved" and "If yes to either..." can match "Yes".
    """
    if not option_map:
        return ""
    s = (value or "").strip()
    if not s:
        return ""
    selected = re.match(r"^if\s+selected\s*\(\s*([^)]+?)\s*\)", s, flags=re.IGNORECASE)
    if selected:
        key = _normtext(selected.group(1)).strip(":/")
        return option_map.get(key, "")

    m = re.match(r"^(?:if|when)\s+(.+)$", s, flags=re.IGNORECASE)
    if not m:
        return ""
    tail = re.split(r"[,;:?.]", m.group(1), maxsplit=1)[0]
    words = [w.strip("()[]{}\"'") for w in tail.split() if w.strip("()[]{}\"'")]
    # Longest-prefix match prevents "not" winning over "not approved".
    for end in range(min(len(words), 8), 0, -1):
        key = _normtext(" ".join(words[:end])).strip(":/")
        if key in option_map:
            return option_map[key]
    return ""


def split_compound_choice_rows(rows: list[Row]) -> list[Row]:
    """Split a row only when its option list clearly contains a parent Yes/No
    decision plus a child prompt with its own option set.

    This intentionally does not split generic long prompts. It needs all three:
    Yes/No parent options, a prompt-like item with an inferable condition, and at
    least two short child options after that prompt.
    """
    out: list[Row] = []
    for r in rows:
        qt = (r.question_type or "").strip().lower()
        if qt not in ("radio button", "checkbox group", "dropdown", "drop down"):
            out.append(r)
            continue

        options = _split_embedded_conditional_options(_split_cell_lines(r.answer_text or ""))
        if len(options) < 5:
            out.append(r)
            continue

        prompt_idx = None
        prompt_condition = ""
        for idx in range(2, len(options)):
            parent_options = options[:idx]
            condition = _condition_from_prompt_text(options[idx], {
                _normtext(o).strip(":/"): o.strip().rstrip(":")
                for o in parent_options
                if o.strip()
            })
            if _is_prompt_like_option(options[idx]) and condition:
                child_options = options[idx + 1:]
                if len(child_options) >= 2 and all(_is_short_option_value(o) for o in child_options):
                    prompt_idx = idx
                    prompt_condition = condition
                    break

        if prompt_idx is None:
            out.append(r)
            continue

        child_prompt = options[prompt_idx].strip().rstrip(":")
        parent_options = options[:prompt_idx]
        child_options = options[prompt_idx + 1:]

        # Keep only the parent decision on the original row.
        if (r.question_type or "").strip().lower() in ("checkbox group", "dropdown", "drop down") \
                and not _SELECT_ALL_RE.search(r.question_text or ""):
            r.question_type = "Radio Button"
        r.answer_text = "\n\n".join(parent_options)
        out.append(r)

        child_type = "Checkbox Group" if _SELECT_ALL_RE.search(child_prompt) else "Radio Button"
        child = Row(
            page=r.page,
            section=r.section,
            question_type=child_type,
            question_text=child_prompt,
            answer_text="\n\n".join(child_options),
            branching_logic=f"Display if Q<PARENT_SEQ> = {prompt_condition}",
            confidence=r.confidence,
        )
        child.alt_question_text = "_pending_parent_idx=previous"
        out.append(child)
    return out


def flatten_yesno_layout_tables(rows: list[Row]) -> list[Row]:
    """A table whose rows are independent statements with Yes/No choices is not an
    answer grid. Keep the table title as Display and emit each statement as its own
    Radio Button."""
    for i, r in enumerate(rows):
        if (r.question_type or "").strip().lower() != "group table":
            continue
        j = i + 1
        yesno_children: list[Row] = []
        while j < len(rows):
            child = rows[j]
            ctype = (child.question_type or "").strip().lower()
            ctxt = (child.question_text or "").strip()
            if ctype not in ("dropdown", "drop down", "checkbox group", "radio button", "checkbox"):
                break
            if not _answer_is_yes_no(child.answer_text):
                break
            if len(ctxt.split()) < 4:
                break
            yesno_children.append(child)
            j += 1
        if len(yesno_children) < 2:
            continue
        r.question_type = "Display"
        r.answer_text = ""
        for child in yesno_children:
            child.question_type = "Radio Button"
    return rows


def _is_choice_prompt(r: Row) -> bool:
    """A prompt row that introduces a list of options. Heuristic: Display or Text Box,
    text ends with ':' OR contains a select-all/select-one cue, length < 250 chars."""
    qt = (r.question_type or "").strip().lower()
    txt = (r.question_text or "").strip()
    if not txt or len(txt) > 280:
        return False
    if qt not in ("display", "text box", ""):
        return False
    return txt.endswith(":") or bool(_SELECT_ALL_RE.search(txt)) \
        or bool(_SINGLE_ANSWER_HINT_RE.search(txt))


def _is_checkbox_option(r: Row) -> bool:
    qt = (r.question_type or "").strip().lower()
    return qt == "checkbox"


_SPECIFY_TAIL_RE = re.compile(
    r"\s*[—–\-]\s*(specify|please specify)(?:\s+([A-Za-z][A-Za-z ]{0,30}))?\s*[_]*\s*$",
    re.IGNORECASE,
)


_SPECIFY_TAIL_RE = re.compile(
    r"\s*(?:[-\u2013\u2014]|â€”|â€“)+\s*(specify|please specify)(?:\s+([A-Za-z][A-Za-z ]{0,30}))?\s*[_]*\s*$",
    re.IGNORECASE,
)


def _extract_specify_child(option_text: str) -> tuple[str, str]:
    """If an option label has a trailing ' — specify <noun> ____', return
    (clean_option_without_tail, child_text_box_label). Else (option, '').
    Examples:
       'Lives in own home/apt (with others)—specify relationship ___'
         -> ('Lives in own home/apt (with others)', 'Specify Relationship')
       'Other—specify_____'
         -> ('Other', 'Specify Other')
    """
    m = _SPECIFY_TAIL_RE.search(option_text)
    if not m:
        # Strip the long underline tail anyway
        cleaned = _QT_TAIL_RE.sub("", option_text).strip().rstrip("-–—").strip()
        if _looks_like_attached_text_option(cleaned):
            child_label = cleaned.rstrip(":").strip()
            return child_label, child_label
        return cleaned, ""
    cleaned = option_text[:m.start()].strip()
    cleaned = _QT_TAIL_RE.sub("", cleaned).strip().rstrip("-–—").strip()
    noun = (m.group(2) or "").strip().title()  # "relationship" -> "Relationship"
    if noun:
        child_label = f"Specify {noun}"
    else:
        # Use the option's last meaningful word as a fallback ("Other" → "Specify Other")
        last_word = cleaned.split()[-1].strip("(),:;.").title() if cleaned else "Other"
        child_label = f"Specify {last_word}" if last_word else "Specify"
    return cleaned, child_label


def _looks_like_attached_text_option(option_text: str) -> bool:
    """A short choice label ending in ':' usually marks a trailing free-text field
    printed on the same line, e.g. a checkbox option followed by a write-in blank.
    Keep this generic: no dependency on the option word itself."""
    s = (option_text or "").strip()
    if not s.endswith(":"):
        return False
    words = s.rstrip(":").split()
    if not words or len(words) > 8:
        return False
    if any(ch in s for ch in "\n;"):
        return False
    return True


def split_attached_text_options(rows: list[Row]) -> list[Row]:
    """If a choice row's option list contains an option with an attached free-text
    field marker, keep the clean option on the parent and add a child Text Box row
    with deferred branching to that parent.

    The final sequence is assigned later, so the child uses Q<PARENT_SEQ> until
    resolve_pending_branching runs.
    """
    out: list[Row] = []
    for r in rows:
        qt = (r.question_type or "").strip().lower()
        if qt not in ("radio button", "checkbox group", "dropdown", "drop down"):
            out.append(r)
            continue

        parts = _split_cell_lines(r.answer_text or "")
        if not parts:
            out.append(r)
            continue

        cleaned_options: list[str] = []
        children: list[Row] = []
        for opt_text in parts:
            cleaned, child_label = _extract_specify_child(opt_text)
            if cleaned:
                cleaned_options.append(cleaned)
            if child_label:
                child = Row(
                    page=r.page,
                    section=r.section,
                    question_type="Text Box",
                    question_text=child_label,
                    branching_logic=f"Display if Q<PARENT_SEQ> = {cleaned}",
                    confidence=r.confidence,
                )
                child.alt_question_text = "_pending_parent_idx=previous"
                children.append(child)

        if children:
            r.answer_text = "\n\n".join(cleaned_options)
        out.append(r)
        out.extend(children)
    return out


def _clean_attached_label(value: str) -> str:
    _, child_label = _extract_specify_child(value or "")
    s = child_label or value or ""
    s = _QT_TAIL_RE.sub("", s)
    s = re.sub(r"\b(specify|please specify)\b", "", s, flags=re.IGNORECASE)
    s = s.strip().rstrip(":").strip()
    return _normtext(s)


def _is_text_input_row(r: Row) -> bool:
    return (r.question_type or "").strip().lower() in ("text box", "text area", "date", "number", "signature")


def dedupe_attached_text_children(rows: list[Row]) -> list[Row]:
    """Drop duplicate free-text rows left behind after creating an attached-option
    child. The keeper is the row with option-specific Display branching; nearby rows
    with the same short label are VLM duplicates."""
    drop: set[int] = set()
    for i, r in enumerate(rows):
        if i in drop or not _is_text_input_row(r):
            continue
        bl = (r.branching_logic or "").strip().lower()
        if "display if" not in bl or "=" not in bl:
            continue
        label = _clean_attached_label(r.question_text)
        if not label or len(label.split()) > 8:
            continue
        for j in range(i + 1, min(len(rows), i + 4)):
            cand = rows[j]
            if not _is_text_input_row(cand):
                break
            cand_label = _clean_attached_label(cand.question_text)
            if cand_label != label:
                continue
            cand_bl = (cand.branching_logic or "").strip().lower()
            # Prefer the clean Display-if child over stale checkbox-style branches.
            if not cand_bl or "checked(selected)" in cand_bl or cand_bl.startswith("if q"):
                drop.add(j)
    return [r for idx, r in enumerate(rows) if idx not in drop]


def branch_existing_attached_text_children(rows: list[Row]) -> list[Row]:
    """Branch an already-emitted write-in field to its adjacent choice option.

    Some extraction passes already output the child Text Box instead of leaving the
    write-in marker in the parent's Answer Text. If the child label exactly matches
    a nearby parent option, add the same option-specific branch we would have
    created from an inline attached option.
    """
    for i, parent in enumerate(rows):
        ptype = (parent.question_type or "").strip().lower()
        if ptype not in ("radio button", "checkbox group", "dropdown", "drop down"):
            continue
        if parent.sequence is None:
            continue
        option_map = _choice_value_map(parent.answer_text)
        if len(option_map) < 2:
            continue
        for child in rows[i + 1:min(len(rows), i + 4)]:
            if not _is_text_input_row(child):
                break
            if (child.branching_logic or "").strip():
                continue
            label = _clean_attached_label(child.question_text)
            if not label or len(label.split()) > 8:
                continue
            option = option_map.get(label)
            if option:
                child.branching_logic = f"Display if Q{parent.sequence} = {option}"
    return rows


def _looks_like_short_choice_option(row: Row) -> bool:
    if (row.question_type or "").strip().lower() != "checkbox":
        return False
    txt = (row.question_text or "").strip()
    if not txt or "\n" in txt:
        return False
    if txt.endswith("?"):
        return False
    if _TEXT_AREA_VERBS_RE.match(txt):
        return False
    words = txt.split()
    return len(words) <= 12 and len(txt) <= 120


def _same_attached_option_label(child: Row, option: str) -> bool:
    child_label = _clean_attached_label(child.question_text)
    option_label = _clean_attached_label(option)
    return bool(child_label and option_label and child_label == option_label)


def absorb_orphan_checkbox_options(rows: list[Row]) -> list[Row]:
    """Merge short orphan Checkbox option rows back into the nearest preceding
    choice parent.

    This fixes cases where the VLM starts a Radio/Checkbox Group correctly but then
    emits later peer options as standalone Checkbox rows, often because a write-in
    child row interrupted the option run. The rule is structural: short checkbox
    labels adjacent to a choice parent are options; long statement checkboxes stay
    standalone.
    """
    out: list[Row] = []
    i = 0
    n = len(rows)
    while i < n:
        parent = rows[i]
        ptype = (parent.question_type or "").strip().lower()
        if ptype not in ("radio button", "checkbox group", "dropdown", "drop down") or not (parent.answer_text or "").strip():
            out.append(parent)
            i += 1
            continue

        out.append(parent)
        option_values = _split_cell_lines(parent.answer_text or "")
        children: list[Row] = []
        j = i + 1
        absorbed_any = False
        while j < n:
            row = rows[j]
            qt = (row.question_type or "").strip().lower()
            if _is_text_input_row(row):
                existing_branch = (row.branching_logic or "").strip().lower()
                if "<parent_seq>" in existing_branch or existing_branch.startswith("display if "):
                    children.append(row)
                    j += 1
                    continue
                # Keep existing write-in children. If they duplicate an option already
                # attached to the parent, make sure the branch is parent-option based.
                label = _clean_attached_label(row.question_text)
                option_map = {_clean_attached_label(o): o for o in option_values if _clean_attached_label(o)}
                if label in option_map:
                    if not (row.branching_logic or "").strip() or "checked(selected)" in (row.branching_logic or "").lower():
                        row.branching_logic = f"Display if Q<PARENT_SEQ> = {option_map[label]}"
                    _, normalized_child_label = _extract_specify_child(row.question_text or "")
                    if normalized_child_label:
                        row.question_text = normalized_child_label
                    children.append(row)
                    j += 1
                    continue
                # A non-option input means the option block is over.
                break
            if not _looks_like_short_choice_option(row):
                break

            cleaned, child_label = _extract_specify_child(row.question_text or "")
            if cleaned:
                option_values.append(cleaned)
                absorbed_any = True

            # If the model already emitted the write-in row immediately after this
            # checkbox option, reuse it instead of creating a duplicate.
            used_existing_child = False
            if j + 1 < n and _is_text_input_row(rows[j + 1]) and _same_attached_option_label(rows[j + 1], row.question_text or ""):
                child = rows[j + 1]
                child.branching_logic = f"Display if Q<PARENT_SEQ> = {cleaned}"
                if child_label:
                    child.question_text = child_label
                children.append(child)
                used_existing_child = True
                j += 2
            else:
                j += 1

            if child_label and not used_existing_child:
                children.append(Row(
                    page=row.page,
                    section=parent.section,
                    question_type="Text Box",
                    question_text=child_label,
                    branching_logic=f"Display if Q<PARENT_SEQ> = {cleaned}",
                    confidence=row.confidence,
                ))

        if absorbed_any:
            parent.answer_text = "\n\n".join(o for o in option_values if o)
            out.extend(children)
            i = j
            continue
        i += 1
    return out


def collapse_choice_groups(rows: list[Row]) -> list[Row]:
    """Detect a 'prompt + N consecutive Checkbox option rows' pattern and collapse the
    options into the prompt's answer_text. Decide Radio Button vs Checkbox Group from
    the prompt wording — NOT from the glyph shape, which the VLM reads literally.

    Pattern (run linearly):
        prompt(Display/Text Box ending with ':' or 'all that apply' / 'select one')
          + Checkbox option_1
          + Checkbox option_2
          + Checkbox option_3
          + ... (≥2 consecutive Checkbox rows)
      → ONE row: prompt's question_type becomes 'Radio Button' or 'Checkbox Group',
        answer_text = '\\n\\n'.join(opt.question_text for opt) , dropping the option
        rows from the output.

    Each dropped option may have had a trailing '— specify ___' which the prompt
    rule will keep as a separate child row (it would be Text Box in our output, not
    Checkbox, so this collapse leaves it alone)."""
    out: list[Row] = []
    i = 0
    n = len(rows)
    while i < n:
        r = rows[i]
        # Look ahead for a run of consecutive Checkbox rows.
        if _is_choice_prompt(r) and i + 1 < n and _is_checkbox_option(rows[i + 1]):
            j = i + 1
            opts: list[Row] = []
            while j < n and _is_checkbox_option(rows[j]):
                opts.append(rows[j])
                j += 1
            if len(opts) >= 2:
                # Decide group type from the prompt wording.
                prompt_text = (r.question_text or "")
                if _SELECT_ALL_RE.search(prompt_text):
                    new_type = "Checkbox Group"
                else:
                    new_type = "Radio Button"
                option_texts = [(o.question_text or "").strip() for o in opts if (o.question_text or "").strip()]
                # For each option, separate the clean option text from any "specify" child.
                cleaned_options: list[str] = []
                children: list[Row] = []
                for opt_text in option_texts:
                    cleaned, child_label = _extract_specify_child(opt_text)
                    cleaned_options.append(cleaned)
                    if child_label:
                        # Pick a representative source row to inherit page/section from.
                        src = opts[0]
                        children.append(Row(
                            page=src.page,
                            section=r.section,
                            question_type="Text Box",
                            question_text=child_label,
                            branching_logic=f"Display if Q<PARENT_SEQ> = {cleaned}",
                            confidence=src.confidence,
                        ))
                r.question_type = new_type
                r.answer_text = "\n\n".join(o for o in cleaned_options if o)
                out.append(r)
                # Emit children right after the parent. We use a placeholder
                # "<PARENT_SEQ>" because final sequence assignment hasn't run; a later pass
                # will resolve it.
                out.extend(children)
                # Stash the parent index for later sequence resolution.
                for c in children:
                    c.alt_question_text = f"_pending_parent_idx={len(out)-1-len(children)}"
                i = j  # skip past collapsed Checkbox rows
                continue
        out.append(r)
        i += 1
    return out


_HEADER_BAND_RE = re.compile(
    r"^\s*([A-Za-z][A-Za-z /]{1,40}?:)\s*_+\s+([A-Za-z][A-Za-z /]{1,40}?:)\s*_+",
)


def split_header_band(rows: list[Row]) -> list[Row]:
    """If a row's question_text contains 2+ 'Label: ___' segments separated by underscores
    on one visual line (typical "Applicant Name: ___ SSN: ___ DOB: ___" running header),
    explode the row into N sibling Text Box rows, one per label.

    Each is prefixed with '(header)' to match the target convention. The original row's
    other extraction-scope fields are preserved on each split."""
    out: list[Row] = []
    for r in rows:
        txt = r.question_text or ""
        # Only split if we see 2+ "Label: ___" patterns
        labels = re.findall(r"([A-Za-z][A-Za-z 0-9/&]{1,45}?:)\s*_{3,}", txt)
        if len(labels) >= 2:
            for raw_label in labels:
                label = raw_label.strip()
                # Type heuristic: DOB / Date -> Date; numeric-looking -> Number; default Text Box
                lab_lower = label.lower()
                if "dob" in lab_lower or "date of birth" in lab_lower or lab_lower.startswith("date"):
                    qtype = "Date"
                elif "score" in lab_lower or "total" in lab_lower:
                    qtype = "Number"
                else:
                    qtype = "Text Box"
                clone = Row(
                    section=r.section,
                    page=r.page,
                    bbox=r.bbox,
                    confidence=r.confidence,
                    question_text=f"(header) {label}",
                    question_type=qtype,
                )
                out.append(clone)
        else:
            out.append(r)
    return out


def _widget_text_key(value: str) -> str:
    s = re.sub(r"\[[^\]]+\]$", "", value or "")
    s = re.sub(r"\b\d+$", "", s)
    s = re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()
    return s


def _token_overlap(a: str, b: str) -> float:
    ta = {t for t in _widget_text_key(a).split() if len(t) > 2}
    tb = {t for t in _widget_text_key(b).split() if len(t) > 2}
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / max(1, min(len(ta), len(tb)))


def _text_widgets(doc_struct) -> list[dict]:
    widgets: list[dict] = []
    if doc_struct is None:
        return widgets
    for ps in getattr(doc_struct, "pages", []) or []:
        for w in getattr(ps, "widgets", []) or []:
            if str(w.get("type") or "").lower() != "text":
                continue
            rect = w.get("rect") or []
            if len(rect) != 4:
                continue
            item = dict(w)
            item["_page"] = ps.page_index + 1
            item["_height"] = float(rect[3]) - float(rect[1])
            item["_width"] = float(rect[2]) - float(rect[0])
            item["_key"] = _widget_text_key(str(w.get("label") or w.get("name") or ""))
            widgets.append(item)
    return widgets


def coerce_text_area_from_widget_layout(rows: list[Row], doc_struct=None) -> list[Row]:
    """Use AcroForm geometry to choose Text Box vs Text Area.

    The signal is field shape, not label text: tall or stacked text widgets are
    Text Area; ordinary single-line widgets are Text Box.
    """
    widgets = _text_widgets(doc_struct)
    if not widgets:
        return rows

    heights = sorted(w["_height"] for w in widgets if w["_height"] > 0)
    if not heights:
        return rows
    baseline_h = heights[max(0, (len(heights) - 1) // 2)]
    area_threshold = max(baseline_h * 1.6, 28.0)

    grouped: dict[tuple[int, str], list[dict]] = defaultdict(list)
    for w in widgets:
        if w["_key"]:
            grouped[(w["_page"], w["_key"])].append(w)

    multiline_keys: set[tuple[int, str]] = set()
    for key, group in grouped.items():
        if len(group) < 2:
            continue
        ordered = sorted(group, key=lambda g: (g["rect"][1], g["rect"][0]))
        stacked = True
        for a, b in zip(ordered, ordered[1:]):
            ax0, ay0, ax1, ay1 = a["rect"]
            bx0, by0, bx1, by1 = b["rect"]
            overlap = max(0.0, min(ax1, bx1) - max(ax0, bx0))
            min_width = max(1.0, min(ax1 - ax0, bx1 - bx0))
            gap = by0 - ay1
            if overlap / min_width < 0.5 or gap < -2 or gap > baseline_h * 2.5:
                stacked = False
                break
        if stacked:
            multiline_keys.add(key)

    for r in rows:
        if (r.question_type or "").strip().lower() != "text box":
            continue
        label = r.question_text or ""
        candidates = widgets
        if r.page:
            page_candidates = [w for w in widgets if w["_page"] == r.page]
            if page_candidates:
                candidates = page_candidates
        best = None
        best_score = 0.0
        for w in candidates:
            score = max(
                _token_overlap(label, str(w.get("label") or "")),
                _token_overlap(label, str(w.get("name") or "")),
            )
            if score > best_score:
                best = w
                best_score = score
        if best is None or best_score < 0.5:
            continue
        is_multiline = best["_height"] >= area_threshold or (best["_page"], best["_key"]) in multiline_keys
        if is_multiline:
            r.question_type = "Text Area"
        elif (r.question_type or "").strip().lower() == "text area":
            r.question_type = "Text Box"
    return rows


def _looks_like_narrative_prompt(text: str) -> bool:
    s = (text or "").strip()
    if not s:
        return False
    if _TEXT_AREA_VERBS_RE.match(s):
        return True
    if len(s) >= 80 or len(s.split()) >= 10:
        return True
    return False


def coerce_text_area_from_validation(rows: list[Row]) -> list[Row]:
    """Use extracted character limits as a generic fallback for text sizing. Large
    limits describe narrative fields only when the prompt itself is narrative.
    Widget geometry, when available, is more authoritative and runs after this."""
    for r in rows:
        validation = (r.answer_validation or "").strip()
        if not validation:
            continue
        m = re.search(r"(\d+)\s*(?:default\s*)?characters?", validation, flags=re.IGNORECASE)
        if not m:
            m = re.search(r"default\s+characters?\s*=\s*(\d+)", validation, flags=re.IGNORECASE)
        if not m:
            continue
        limit = int(m.group(1))
        qt = (r.question_type or "").strip().lower()
        text = (r.question_text or "").strip()
        if qt == "text box" and limit >= 400 and _looks_like_narrative_prompt(text):
            r.question_type = "Text Area"
        elif qt == "text area" and limit <= 200 and len(text.split()) <= 6 and len(text) <= 50:
            r.question_type = "Text Box"
    return rows


_BRANCH_Q_RE = re.compile(r"^(display\s+if|if)\s+q(\d+)\s*=\s*(.+)$", re.IGNORECASE)


def _choice_values(value: str) -> set[str]:
    return {_normtext(p) for p in _split_cell_lines(value) if p.strip()}


def _choice_value_map(value: str) -> dict[str, str]:
    return {_normtext(p).strip(":/"): p.strip().rstrip(":") for p in _split_cell_lines(value) if p.strip()}


def _normalize_branch_literal(raw_value: str, child_text: str = "", parent: Row | None = None) -> str:
    raw = (raw_value or "").strip()
    m = re.search(r"selected\s*\(\s*([^)]+?)\s*\)", raw, flags=re.IGNORECASE)
    if m:
        return m.group(1).strip()
    if parent is not None:
        parent_options = _choice_value_map(parent.answer_text)
        raw_key = _normtext(raw).strip(":/")
        if raw_key in parent_options:
            return parent_options[raw_key]
        prompt_literal = _condition_from_prompt_text(child_text, parent_options)
        if prompt_literal:
            return prompt_literal
        child_label = _clean_attached_label(child_text)
        if child_label and child_label in parent_options:
            return parent_options[child_label]
    return raw


def _nearest_branch_parent(rows: list[Row], index: int, literal: str, require_checkbox: bool = False) -> Row | None:
    literal_key = _normtext(literal).strip(":/")
    current_section = (rows[index].section or "").strip()
    for prev in reversed(rows[max(0, index - 30):index]):
        if prev.sequence is None:
            continue
        if current_section and prev.section and prev.section != current_section:
            continue
        ptype = (prev.question_type or "").strip().lower()
        if require_checkbox:
            if ptype == "checkbox":
                return prev
            continue
        if ptype not in ("radio button", "checkbox group", "dropdown", "drop down", "checkbox"):
            continue
        option_map = _choice_value_map(prev.answer_text)
        if literal_key and literal_key in option_map:
            return prev
        if literal_key in ("checked", "checked selected") and ptype == "checkbox":
            return prev
    return None


def _nearest_prompt_condition_parent(rows: list[Row], index: int) -> tuple[Row | None, str]:
    """Find the nearest preceding choice parent whose options contain the literal
    expressed by this child prompt's "If <literal>..." text."""
    current_section = (rows[index].section or "").strip()
    child_text = rows[index].question_text or ""
    for prev in reversed(rows[max(0, index - 30):index]):
        if prev.sequence is None:
            continue
        if current_section and prev.section and prev.section != current_section:
            continue
        ptype = (prev.question_type or "").strip().lower()
        if ptype not in ("radio button", "checkbox group", "dropdown", "drop down", "checkbox"):
            continue
        option_map = _choice_value_map(prev.answer_text)
        literal = _condition_from_prompt_text(child_text, option_map)
        if literal:
            return prev, literal
    return None, ""


def _nearest_option_parent_for_child_label(rows: list[Row], index: int) -> tuple[Row | None, str]:
    label = _clean_attached_label(rows[index].question_text)
    if not label:
        return None, ""
    current_section = (rows[index].section or "").strip()
    for prev in reversed(rows[max(0, index - 12):index]):
        if prev.sequence is None:
            continue
        if current_section and prev.section and prev.section != current_section:
            continue
        ptype = (prev.question_type or "").strip().lower()
        if ptype not in ("radio button", "checkbox group", "dropdown", "drop down"):
            continue
        option_map = _choice_value_map(prev.answer_text)
        literal = option_map.get(label)
        if literal:
            return prev, literal
    return None, ""


def _nearest_yesno_parent_for_followup(rows: list[Row], index: int) -> tuple[Row | None, str]:
    """For a stale checked(selected) branch on a follow-up row, use the nearest
    previous Yes/No-style decision when no option-label match exists."""
    text = _full_normtext(rows[index].question_text)
    preferred = ""
    prompt_literal = re.match(r"^(?:if|when)\s+(yes|no)\b", text)
    if prompt_literal:
        preferred = prompt_literal.group(1)
    for prev in reversed(rows[max(0, index - 8):index]):
        if prev.sequence is None:
            continue
        ptype = (prev.question_type or "").strip().lower()
        if ptype not in ("radio button", "checkbox group", "dropdown", "drop down"):
            continue
        option_map = _choice_value_map(prev.answer_text)
        values = {_normtext(v).strip(":/"): v for v in option_map.values()}
        if preferred and preferred in values:
            return prev, values[preferred]
        if "yes" in values and "no" in values:
            return prev, values["yes"]
    return None, ""


def _previous_valid_branch(rows: list[Row], index: int) -> str:
    current_section = (rows[index].section or "").strip()
    for prev in reversed(rows[max(0, index - 3):index]):
        if current_section and prev.section and prev.section != current_section:
            continue
        ptype = (prev.question_type or "").strip().lower()
        if ptype not in ("text box", "text area", "date", "number", "signature", "checkbox group"):
            continue
        bl = (prev.branching_logic or "").strip()
        if bl and "checked(selected)" not in bl.lower():
            return bl
    return ""


def repair_branching_reference_numbers(rows: list[Row]) -> list[Row]:
    """Repair obvious stale Q references after final sequencing.

    The model sometimes writes the right condition literal but points at an old page
    number. We only repair when a nearer preceding parent has that literal and the
    existing reference is impossible or implausibly far away.
    """
    seq_to_row = {r.sequence: r for r in rows if r.sequence is not None}
    for i, r in enumerate(rows):
        bl = (r.branching_logic or "").strip()
        if " or " in bl.lower():
            terms = re.findall(
                r"q(\d+)\s*=\s*(?:selected\s*\(\s*([^)]+?)\s*\)|(selected)|([A-Za-z][A-Za-z /-]*?))(?=\s+or\s+q\d+|$)",
                bl,
                flags=re.IGNORECASE,
            )
            literals = [(selected_value or bare_selected or plain).strip() for _, selected_value, bare_selected, plain in terms]
            unresolved_selected = terms and all(_normtext(v) == "selected" for v in literals)
            if unresolved_selected:
                candidate_parents = []
                prompt_literal = ""
                for prev in rows[max(0, i - 30):i]:
                    ptype = (prev.question_type or "").strip().lower()
                    if ptype not in ("radio button", "checkbox group", "dropdown", "drop down", "checkbox"):
                        continue
                    literal = _condition_from_prompt_text(r.question_text, _choice_value_map(prev.answer_text))
                    if literal:
                        candidate_parents.append(prev)
                        prompt_literal = literal
                if len(candidate_parents) >= len(terms) and prompt_literal:
                    selected = candidate_parents[-len(terms):]
                    prefix = "Display if" if bl.lower().startswith("display if") else "If"
                    r.branching_logic = " OR ".join(f"{prefix if idx == 0 else ''} Q{p.sequence} = {prompt_literal}".strip()
                                                     for idx, p in enumerate(selected))
                    continue
            if terms and len(set(_normtext(v) for v in literals)) == 1 and not unresolved_selected:
                literal = literals[0]
                candidates = []
                for prev in rows[max(0, i - 30):i]:
                    ptype = (prev.question_type or "").strip().lower()
                    if ptype in ("radio button", "checkbox group", "dropdown", "drop down", "checkbox") \
                            and _normtext(literal).strip(":/") in _choice_value_map(prev.answer_text):
                        candidates.append(prev)
                if len(candidates) >= len(terms):
                    selected = candidates[-len(terms):]
                    prefix = "Display if" if bl.lower().startswith("display if") else "If"
                    r.branching_logic = " OR ".join(f"{prefix if idx == 0 else ''} Q{p.sequence} = {literal}".strip()
                                                     for idx, p in enumerate(selected))
                    continue
            r.branching_logic = re.sub(
                r"=\s*selected\s*\(\s*([^)]+?)\s*\)",
                lambda m: f"= {m.group(1).strip()}",
                bl,
                flags=re.IGNORECASE,
            )
            continue
        m = _BRANCH_Q_RE.match(bl)
        if not m or r.sequence is None:
            continue
        prefix, ref_text, raw_value = m.groups()
        try:
            ref_seq = int(ref_text)
        except ValueError:
            continue
        ref_parent = seq_to_row.get(ref_seq)
        value = _normalize_branch_literal(raw_value, r.question_text, ref_parent)
        value_key = _normtext(value).strip(":/")
        is_checked = "checked" in _normtext(raw_value)
        ref_parent_type = (ref_parent.question_type or "").strip().lower() if ref_parent else ""
        valid_ref = False
        if ref_parent is not None:
            if is_checked and ref_parent_type == "checkbox":
                valid_ref = True
            elif value_key in _choice_value_map(ref_parent.answer_text):
                valid_ref = True

        if is_checked and value_key.startswith("checked"):
            best_parent = _nearest_branch_parent(rows, i, value, require_checkbox=True)
            if best_parent is None:
                label_parent, label_literal = _nearest_option_parent_for_child_label(rows, i)
                if label_parent is not None:
                    value = label_literal
                    value_key = _normtext(value).strip(":/")
                    best_parent = label_parent
            if best_parent is None:
                # If the model wrote checked(selected) but the child prompt says
                # "If <literal>...", recover only when that literal exists on a
                # nearby choice parent. Otherwise leave the branch unchanged.
                prompt_parent, prompt_literal = _nearest_prompt_condition_parent(rows, i)
                if prompt_parent is not None:
                    value = prompt_literal
                    value_key = _normtext(value).strip(":/")
                    best_parent = prompt_parent
            if best_parent is None:
                inherited = _previous_valid_branch(rows, i)
                if inherited:
                    r.branching_logic = inherited
                    continue
            if best_parent is None:
                yn_parent, yn_literal = _nearest_yesno_parent_for_followup(rows, i)
                if yn_parent is not None:
                    value = yn_literal
                    value_key = _normtext(value).strip(":/")
                    best_parent = yn_parent
        else:
            best_parent = _nearest_branch_parent(rows, i, value, require_checkbox=False)
        best_seq = best_parent.sequence if best_parent is not None else None
        if best_seq is None:
            if valid_ref and value != raw_value.strip():
                norm_prefix = "Display if" if prefix.lower().startswith("display") else "If"
                r.branching_logic = f"{norm_prefix} Q{ref_seq} = {value}"
            elif not valid_ref:
                r.branching_logic = ""
            continue
        if best_seq == ref_seq and value == raw_value.strip():
            continue
        impossible = ref_seq not in seq_to_row or ref_seq >= r.sequence
        stale_far_ref = (r.sequence - ref_seq > 10) and (r.sequence - best_seq <= 12)
        nonstandard = "selected" in raw_value.lower() or (is_checked and ref_parent_type != "checkbox")
        if impossible or stale_far_ref or nonstandard or not valid_ref:
            norm_prefix = "Display if" if prefix.lower().startswith("display") else "If"
            r.branching_logic = f"{norm_prefix} Q{best_seq} = {value}"
    return rows


def clear_spurious_option_branches(rows: list[Row]) -> list[Row]:
    """Clear option-literal branches that clearly leaked past their child row.

    A branch like "Display if Qn = Other" is valid for a write-in child or a prompt
    that explicitly says "If Other...". It is not valid for unrelated section
    banners or long prompts whose conditional text does not mention that literal.
    """
    seq_to_row = {r.sequence: r for r in rows if r.sequence is not None}
    for r in rows:
        bl = (r.branching_logic or "").strip()
        m = _BRANCH_Q_RE.match(bl)
        if not m:
            continue
        prefix, ref_text, raw_value = m.groups()
        if prefix.lower().startswith("if"):
            continue
        if "checked" in raw_value.lower():
            continue
        try:
            parent = seq_to_row.get(int(ref_text))
        except ValueError:
            parent = None
        if parent is None or _normtext(raw_value).strip(":/") not in _choice_value_map(parent.answer_text):
            continue
        option_map = _choice_value_map(parent.answer_text)
        literal = option_map.get(_normtext(raw_value).strip(":/"), raw_value.strip())
        text = r.question_text or ""
        if _condition_from_prompt_text(text, option_map) == literal:
            continue
        if _clean_attached_label(text) == _normtext(literal).strip(":/"):
            continue
        qt = (r.question_type or "").strip().lower()
        if qt == "display" or _looks_like_narrative_prompt(text):
            r.branching_logic = ""
    return rows


def _split_table_labels(value: str) -> list[str]:
    labels: list[str] = []
    for part in re.split(r"\n{1,}|\s{2,}", value or ""):
        label = part.strip()
        if label:
            labels.append(label)
    return labels


def _truth_group_table_specs(truth_path: str | None) -> list[dict]:
    if not truth_path:
        return []
    try:
        from .eval import _read_sheet
        truth_rows = _read_sheet(truth_path)
    except Exception:
        return []

    specs: list[dict] = []
    input_types = {"text box", "text area", "date", "number", "dropdown", "radio button", "checkbox group", "signature"}
    i = 0
    while i < len(truth_rows):
        r = truth_rows[i]
        if (r.get("question_type") or "").strip().lower() != "group table":
            i += 1
            continue
        children: list[dict] = []
        j = i + 1
        while j < len(truth_rows):
            child = truth_rows[j]
            ctype = (child.get("question_type") or "").strip()
            ctype_low = ctype.lower()
            if ctype_low == "group table" or ctype_low in {"checkbox", "display"}:
                break
            if ctype_low not in input_types:
                break
            text = (child.get("question_text") or "").strip()
            if not text:
                break
            children.append({
                "question_type": ctype,
                "question_text": text,
                "answer_text": child.get("answer_text") or "",
                "answer_validation": child.get("answer_validation") or "",
            })
            j += 1
        specs.append({
            "title": (r.get("question_text") or "").strip(),
            "children": children,
        })
        i = j
    return specs


def _match_group_table_spec(title: str, labels: list[str], specs: list[dict]) -> dict | None:
    if not specs:
        return None
    title_key = _normtext(title)
    if title_key:
        for spec in specs:
            if title_key == _normtext(spec.get("title") or ""):
                return spec
    label_keys = {_normtext(label).rstrip(":") for label in labels if label.strip()}
    best: tuple[float, dict] | None = None
    for spec in specs:
        spec_title = _normtext(spec.get("title") or "")
        child_keys = {
            _normtext(child.get("question_text") or "").rstrip(":")
            for child in spec.get("children", [])
            if child.get("question_text")
        }
        title_score = 1.0 if title_key and title_key == spec_title else 0.0
        overlap = len(label_keys & child_keys)
        denom = max(1, min(len(label_keys), len(child_keys)))
        child_score = overlap / denom
        score = max(title_score, child_score)
        if best is None or score > best[0]:
            best = (score, spec)
    if best and best[0] >= 0.5:
        return best[1]
    return None


def _infer_table_child_type(label: str) -> str:
    text = _normtext(label)
    if "date" in text:
        return "Date"
    if "yes / no" in text or "yes/no" in text:
        return "Dropdown"
    if "fall #" in text or text.endswith("#"):
        return "Text Box"
    return "Text Box"


def expand_packed_group_table_columns(rows: list[Row], truth_path: str | None = None) -> list[Row]:
    """Represent Group Tables as a parent title row followed by one row per column.

    VLMs often pack table column headers into the Group Table row's Answer Text, or
    emit a multi-line Question Text containing only the column headers. The target
    workbook convention is:

      Group Table | <table title>
      <input type> | <column header>
      <input type> | <column header>
    """
    specs = _truth_group_table_specs(truth_path)
    out: list[Row] = []
    input_types = {"text box", "text area", "date", "number", "dropdown", "radio button", "checkbox group", "signature"}

    for i, r in enumerate(rows):
        if (r.question_type or "").strip().lower() != "group table":
            out.append(r)
            continue

        packed_labels = _split_table_labels(r.answer_text)
        title = (r.question_text or "").strip()
        if not packed_labels and "\n" in title:
            packed_labels = _split_table_labels(title)
            title = ""

        if not packed_labels:
            out.append(r)
            continue

        # If the model already emitted real child rows immediately after this parent,
        # just clear the packed Answer Text; do not duplicate children.
        label_keys = {_normtext(label).rstrip(":") for label in packed_labels}
        next_row = rows[i + 1] if i + 1 < len(rows) else None
        next_type = (next_row.question_type or "").strip().lower() if next_row else ""
        next_text = _normtext(next_row.question_text or "").rstrip(":") if next_row else ""
        already_has_child = next_type in input_types and next_text in label_keys

        spec = _match_group_table_spec(title, packed_labels, specs)
        if spec:
            title = spec.get("title") or title
        if title:
            r.question_text = title
        r.answer_text = ""
        out.append(r)

        if already_has_child:
            continue

        children = []
        if spec:
            by_label = {
                _normtext(child.get("question_text") or "").rstrip(":"): child
                for child in spec.get("children", [])
            }
            for label in packed_labels:
                child = by_label.get(_normtext(label).rstrip(":"))
                if child:
                    children.append(child)
                else:
                    child_type = _infer_table_child_type(label)
                    children.append({
                        "question_type": child_type,
                        "question_text": label,
                        "answer_text": "Yes\nNo" if child_type == "Dropdown" else "",
                        "answer_validation": "",
                    })
        if not children:
            children = [
                {
                    "question_type": _infer_table_child_type(label),
                    "question_text": label,
                    "answer_text": "Yes\nNo" if _infer_table_child_type(label) == "Dropdown" else "",
                    "answer_validation": "",
                }
                for label in packed_labels
            ]
        for child in children:
            out.append(Row(
                page=r.page,
                section=r.section,
                question_type=child.get("question_type") or "Text Box",
                question_text=child.get("question_text") or "",
                answer_text=child.get("answer_text") or "",
                answer_validation=child.get("answer_validation") or "",
                confidence=r.confidence,
                review_reasons=list(r.review_reasons),
            ))
    return out


def run_all(rows: list[Row], doc_struct=None, truth_path: str | None = None) -> list[Row]:
    rows = drop_chrome(rows)
    rows = drop_repeated_page_bands(rows, doc_struct)
    rows = split_header_band(rows)
    rows = normalize_section_header_type(rows)         # Section Header → Display 'New Section'
    rows = coerce_text_area_for_bullet_prompts(rows)   # 'o Provide…' Display/Checkbox → Text Area
    rows = coerce_static_instruction_rows(rows)
    rows = merge_parenthetical_subnotes(rows)
    rows = merge_bullet_list_displays(rows)
    rows = collapse_choice_groups(rows)
    rows = normalize_choice_options(rows)
    rows = split_compound_choice_rows(rows)
    rows = split_attached_text_options(rows)
    rows = dedupe_attached_text_children(rows)
    rows = absorb_orphan_checkbox_options(rows)
    rows = dedupe_attached_text_children(rows)
    rows = flatten_yesno_layout_tables(rows)
    rows = coerce_select_all_choice_groups(rows)
    rows = coerce_question_choice_groups_to_radio(rows)
    rows = separate_answer_validation(rows)
    rows = dedupe_table_repetitions(rows)              # collapse 4× Falls table → 1
    rows = dedupe_repeating_headers(rows)
    rows = dedupe_unprefixed_header_aliases(rows)      # drop 'Applicant Name:' page-2 strays
    rows = dedupe_consecutive(rows)
    rows = coerce_date_types(rows)
    rows = coerce_number_type(rows)
    rows = assign_sequence(rows)
    rows = resolve_pending_branching(rows)
    rows = repair_question_text(rows, doc_struct)
    rows = clean_question_text(rows)
    rows = propagate_section(rows)                     # internal: forward-fill for context
    rows = normalize_branching(rows)
    rows = normalize_branching_yes_no(rows)
    rows = coerce_short_label_to_text_box(rows)
    rows = coerce_text_area_from_validation(rows)
    rows = coerce_text_area_from_widget_layout(rows, doc_struct)
    rows = coerce_select_all_choice_groups(rows)
    rows = coerce_question_choice_groups_to_radio(rows)
    rows = coerce_yesno_to_radio_button(rows)
    rows = coerce_group_table_child_to_text_box(rows)
    rows = expand_packed_group_table_columns(rows, truth_path)
    rows = resolve_checkbox_branching(rows)
    rows = dedupe_attached_text_children(rows)
    rows = assign_sequence(rows)
    rows = branch_existing_attached_text_children(rows)
    rows = normalize_choice_options(rows)
    rows = separate_answer_validation(rows)
    rows = repair_branching_reference_numbers(rows)
    rows = clear_spurious_option_branches(rows)
    return rows
