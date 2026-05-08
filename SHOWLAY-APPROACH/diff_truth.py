"""
Cell-level diff: for each matched (candidate, truth) pair, show every disagreement.
Helps find systematic patterns (e.g. model says "Text Box" where truth says "Calendar"
for date fields, or model joins answer options with \\n where truth uses \\n\\n).
"""
from __future__ import annotations
import sys
from pathlib import Path
from showlay.eval import _read_sheet, _greedy_match, _norm, _token_ratio

if len(sys.argv) < 3:
    print("usage: python diff_truth.py <candidate.xlsx> <truth.xlsx> [--limit N]")
    sys.exit(1)

cand = sys.argv[1]
truth = sys.argv[2]
limit = int(sys.argv[sys.argv.index("--limit") + 1]) if "--limit" in sys.argv else 60

cand_rows = _read_sheet(cand)
truth_rows = _read_sheet(truth)
matches = _greedy_match(cand_rows, truth_rows)

print(f"\n=== {Path(cand).name}  vs  {Path(truth).name} ===")
print(f"cand={len(cand_rows)}  truth={len(truth_rows)}  matched={len(matches)}\n")

FIELDS = ["question_type", "question_text", "section", "answer_text", "branching_logic"]

# Per-field disagreements catalogued
from collections import Counter
disagreements: dict[str, list] = {f: [] for f in FIELDS}

for ci, ti, score in sorted(matches, key=lambda x: -x[0]):
    c, t = cand_rows[ci], truth_rows[ti]
    diffs = []
    for f in FIELDS:
        cv, tv = (c[f] or "").strip(), (t[f] or "").strip()
        if _norm(cv) != _norm(tv):
            diffs.append((f, cv, tv))
            disagreements[f].append((cv, tv))
    if diffs:
        print(f"--- match score={score:.2f} ---")
        print(f"  truth: type={t['question_type']!r:<14} qt={t['question_text'][:60]!r}")
        print(f"   cand: type={c['question_type']!r:<14} qt={c['question_text'][:60]!r}")
        for f, cv, tv in diffs:
            print(f"     [{f}]  truth={tv[:80]!r}\n              cand={cv[:80]!r}")
        print()

print("\n========= disagreement counts =========")
for f, items in disagreements.items():
    print(f"\n[{f}]  ({len(items)} disagreements)")
    # Show top patterns
    patterns = Counter(((cv[:40] if cv else "<empty>"), (tv[:40] if tv else "<empty>")) for cv, tv in items)
    for (cv, tv), n in patterns.most_common(10):
        print(f"  {n:>3}x  cand={cv!r:<42}  truth={tv!r}")
