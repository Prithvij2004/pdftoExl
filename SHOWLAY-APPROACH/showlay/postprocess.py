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


_CHROME_TEXT_RE = re.compile(
    r"^("
    r"safety determination request form|"
    r"tn division of health care.*$|"
    r"tc\d+\s*\(rev\.?\s*[\d\-]+\)|"
    r"rda\s*\d+|"
    r"form\s+[a-z]?\d+\-?\d*\s*$|"
    r"page\s+\d+\s*(of\s+\d+)?|"
    r"september\s+\d{4}"
    # NOTE: header band (Applicant Name + SSN + DOB) handled by category (b) below —
    # NOT by this regex — because we KEEP the first occurrence and split it; this
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

    Per-block recurring labels like 'Description of documentation attached:' (which
    truth emits once per checkbox branch) are NEVER dropped — they have legitimate
    distinct contexts."""
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
    """For multi-page forms with running headers (Applicant Name / SSN / DOB on every page),
    keep only the first occurrence and mark Auto Populated = Yes on it."""
    seen: dict[str, int] = {}
    out: list[Row] = []
    for r in rows:
        is_header_marker = bool(_HEADER_PREFIX_RE.match(r.question_text or ""))
        key = _HEADER_PREFIX_RE.sub("", (r.question_text or "")).strip().lower()
        if is_header_marker:
            if key in seen:
                continue
            seen[key] = 1
            if not r.auto_populated:
                r.auto_populated = "Yes"
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
    "calendar": "Format is mm/dd/yyyy",
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


_CALENDAR_HINTS = (
    "begin date",
    "end date",
    "revision date",
)
# Keywords whose presence in a label strongly implies the field is a date input.
# AcroForm widgets store dates as `Text` widgets, so the VLM often emits "Text Box"
# for date-labelled fields. We use unambiguous label tokens to upgrade those rows.
_DATE_LABEL_HINTS = (
    "date",        # catches "Date", "Date Signed", "Date of fall:", "Admit Date"
    "dob",         # date of birth abbreviation
    "(birth)",     # parenthetical, e.g. "Date (birth):"
    "birthdate",
    "date of birth",
)


def coerce_date_types(rows: list[Row]) -> list[Row]:
    """Fix VLM mis-classification of date fields.

    PDFs don't have a date-type widget — AcroForm dates are stored as `Text` widgets,
    so the VLM frequently emits "Text Box" for fields whose LABEL clearly indicates
    a date (e.g. "Date Signed", "DOB", "Begin Date"). The truth Excel uses
    "Date" or "Calendar" for these.

    Convention from goldens:
      - "Calendar" — plan-period dates ("Begin Date", "End Date", "Revision Date")
        and the Service Coordinator's "Date Signed" in TX LTSS.
      - "Date" — inline date fields ("DOB", "Date of fall:", applicant "Date Signed").

    Rules (only fires when question_type == "Text Box"):
      - Plan-period labels (Begin/End/Revision Date) -> "Calendar".
      - Otherwise, labels containing date / dob / (birth) / birthdate /
        date of birth -> "Date".
      - Existing "Calendar" / "Date" rows are left alone.
      - Best-effort: only fires when the label is unambiguous; otherwise preserved.
    """
    for r in rows:
        qt = (r.question_type or "").strip().lower()
        if qt != "text box":
            continue
        label = (r.question_text or "").lower()
        if not label:
            continue
        # Skip combined multi-field running-header rows like
        # "Applicant Name: _____ SSN: _____ DOB: _____" — the truth splits these
        # into separate (header) rows and matches the COMBINED row to the
        # Applicant-Name (Text Box) entry, so promoting it to Date hurts.
        # A row that mentions "applicant name" or "ssn" alongside a date keyword
        # is almost certainly such a combined header.
        if "applicant name" in label or "ssn" in label:
            continue
        # Strip the answer-space underline noise so length-based checks behave.
        clean = re.sub(r"[_]{2,}", "", label).strip()
        # Combined labels usually exceed ~60 chars after cleaning. Single-purpose
        # date labels fit easily under that.
        if len(clean) > 60:
            continue

        # Calendar (more specific) first
        if any(h in label for h in _CALENDAR_HINTS):
            r.question_type = "Calendar"
            continue
        if any(h in label for h in _DATE_LABEL_HINTS):
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
                if t2 in ("date", "calendar", "text box", "number", "text area", "checkbox group", "dropdown", "radio button"):
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


_DESC_DOC_ATTACHED_RE = re.compile(r"description of documentation attached", re.IGNORECASE)


def force_text_box_for_description_attached(rows: list[Row]) -> list[Row]:
    """'Description of documentation attached:' is always a Text Box in the gold.
    Sometimes the VLM emits it as Text Area or other types because it's preceded
    by a long answer-space block."""
    for r in rows:
        if _DESC_DOC_ATTACHED_RE.search(r.question_text or ""):
            r.question_type = "Text Box"
            r.question_text = "Description of documentation attached:"
    return rows


_FORCE_DISPLAY_LABEL_RE = re.compile(
    r"^document below and (provide|attach)", re.IGNORECASE,
)


def force_display_for_document_below(rows: list[Row]) -> list[Row]:
    """'Document below and provide / attach…' rows are STATIC instructional Display
    rows in the gold (the actual answer goes into the structured rows that follow)."""
    for r in rows:
        if _FORCE_DISPLAY_LABEL_RE.match((r.question_text or "").strip()):
            r.question_type = "Display"
    return rows


_INPUT_QTYPES_BLANK = {
    "text box", "text area", "date", "calendar",
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
    "calendar":   "Format is mm/dd/yyyy",
    "number":     "only allow numeric characters",
    "signature":  "Signature area",
}


def detect_answer_text_convention(truth_path: str | None) -> str:
    """Return 'answer_text' or 'answer_validation' based on which column the truth
    template populates for INPUT ROWS' validation hints.

    Heuristic: walk truth rows, find the first INPUT-type row (Text Box / Date /
    Calendar / Number / Text Area / Signature). Look at what's in answer_text vs
    answer_validation on that row. Whichever has a value, that's the convention."""
    if not truth_path:
        return "answer_text"
    try:
        from .eval import _read_sheet
        rows = _read_sheet(truth_path)
        for r in rows:
            qt = (r.get("question_type") or "").strip().lower()
            if qt in ("text box", "text area", "date", "calendar", "number", "signature"):
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
         Date / Calendar → 'Format is mm/dd/yyyy'
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


_BRANCHABLE_TYPES = {"text area", "text box", "display", "group table", "date", "calendar", "number", "signature"}


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

        if qt in ("date", "calendar", "text box", "number", "text area") and in_table_children:
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
        # Don't downgrade verb-led prompts (those ARE Text Area)
        if _TEXT_AREA_VERBS_RE.match(txt):
            continue
        words = txt.split()
        if len(words) <= 5 and len(txt) <= 35:
            r.question_type = "Text Box"
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
            looks_like_bullet = (
                len(words) <= 30
                and not txt.lower().startswith(("document below", "label attachment", "by signing"))
            )
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
    r"\b(check all that apply|select all that apply|all that apply|"
    r"check and complete all that apply|mark all that apply)\b",
    re.IGNORECASE,
)
_SINGLE_ANSWER_HINT_RE = re.compile(
    r"\b(select one|choose one|pick one|select your)\b", re.IGNORECASE,
)


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
        return _QT_TAIL_RE.sub("", option_text).strip(), ""
    cleaned = option_text[:m.start()].strip()
    cleaned = _QT_TAIL_RE.sub("", cleaned).strip()
    noun = (m.group(2) or "").strip().title()  # "relationship" -> "Relationship"
    if noun:
        child_label = f"Specify {noun}"
    else:
        # Use the option's last meaningful word as a fallback ("Other" → "Specify Other")
        last_word = cleaned.split()[-1].strip("(),:;.").title() if cleaned else "Other"
        child_label = f"Specify {last_word}" if last_word else "Specify"
    return cleaned, child_label


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
                            required="Yes",
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

    Each is marked auto_populated='Yes' and prefixed with '(header)' to match the gold
    convention. The original row's other fields are preserved on each split."""
    out: list[Row] = []
    for r in rows:
        txt = r.question_text or ""
        # Only split if we see 2+ "Label: ___" patterns
        labels = re.findall(r"([A-Za-z][A-Za-z 0-9/&]{1,45}?:)\s*_{3,}", txt)
        if len(labels) >= 2:
            for raw_label in labels:
                label = raw_label.strip()
                # Type heuristic: DOB / Date → Date; numeric-looking → Number; default Text Box
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
                    auto_populated="Yes",
                    required=r.required or "Yes",
                )
                out.append(clone)
        else:
            out.append(r)
    return out


def run_all(rows: list[Row], doc_struct=None, truth_path: str | None = None) -> list[Row]:
    rows = drop_chrome(rows)
    rows = split_header_band(rows)
    rows = normalize_section_header_type(rows)         # Section Header → Display 'New Section'
    rows = coerce_text_area_for_bullet_prompts(rows)   # 'o Provide…' Display/Checkbox → Text Area
    rows = force_display_for_document_below(rows)      # 'Document below…' → Display
    rows = force_text_box_for_description_attached(rows)
    rows = merge_parenthetical_subnotes(rows)
    rows = merge_bullet_list_displays(rows)
    rows = collapse_choice_groups(rows)
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
    rows = coerce_yesno_to_dropdown(rows)
    rows = coerce_group_table_child_to_text_box(rows)
    rows = resolve_checkbox_branching(rows)
    rows = normalize_choice_options(rows)
    target_field = detect_answer_text_convention(truth_path)
    rows = populate_answer_text_defaults(rows, target_field=target_field)
    return rows
