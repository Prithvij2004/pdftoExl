"""Eval D — semantic equivalence metrics.

Implements:
  - branching_logic_equivalence (AST equality via dsl.canonicalize)
  - question_text_equivalence   (exact first, then LLM judge on residual)
  - answer_text_equivalence     (same; checkbox options compared as sets)
  - equivalence_uplift_over_eval_B (diagnostic)

The LLM judge is a Protocol. In tests it is stubbed. A real Bedrock client is
provided but gated: it is only constructed when the caller passes
`enable_d=True` (typically from the `--enable-d` CLI flag or the
`PDFTOXL_ENABLE_D` env var). Otherwise the default judge returns `"unknown"`,
which flags the affected cells as requiring enablement.
"""
from __future__ import annotations

import hashlib
import os
import unicodedata
from dataclasses import dataclass, field
from typing import Iterable, Literal, Optional, Protocol, Sequence

from ..dsl import DSLParseError, canonicalize


JudgeVerdict = Literal["yes", "no", "unknown"]

JUDGE_PROMPT_VERSION = "d-v1"
JUDGE_PROMPT_TEMPLATE = (
    'You are evaluating whether two strings refer to the same question on the same form.\n'
    'String A: {a}\n'
    'String B: {b}\n'
    'Answer "yes" if and only if a reasonable form-reviewer would treat them as the same question.\n'
    'Answer "no" otherwise.\n'
    'Output exactly one line: "yes" or "no", followed by a one-sentence reason.\n'
)


class Judge(Protocol):
    """Pluggable LLM judge. Implementations must be deterministic for a given seed."""

    def judge(self, candidate: str, reference: str) -> JudgeVerdict: ...


@dataclass
class StubJudge:
    """Deterministic in-memory judge driven by a pre-programmed lookup.

    `responses` is keyed by `(candidate, reference)` and returns "yes"/"no".
    Pairs not in the map return `default`.
    """

    responses: dict[tuple[str, str], JudgeVerdict] = field(default_factory=dict)
    default: JudgeVerdict = "unknown"
    calls: list[tuple[str, str]] = field(default_factory=list)

    def judge(self, candidate: str, reference: str) -> JudgeVerdict:
        self.calls.append((candidate, reference))
        return self.responses.get((candidate, reference), self.default)


@dataclass
class DisabledJudge:
    """Default judge when Eval D is not enabled. Always returns 'unknown'."""

    def judge(self, candidate: str, reference: str) -> JudgeVerdict:  # noqa: ARG002
        return "unknown"


@dataclass
class CachingJudge:
    """Wraps another judge and memoizes by content hash."""

    inner: Judge
    _cache: dict[str, JudgeVerdict] = field(default_factory=dict)

    @staticmethod
    def _key(candidate: str, reference: str) -> str:
        h = hashlib.sha256()
        h.update(candidate.encode("utf-8"))
        h.update(b"\x00")
        h.update(reference.encode("utf-8"))
        return h.hexdigest()

    def judge(self, candidate: str, reference: str) -> JudgeVerdict:
        k = self._key(candidate, reference)
        if k in self._cache:
            return self._cache[k]
        v = self.inner.judge(candidate, reference)
        # Cache only definitive verdicts — "unknown" may become knowable later.
        if v in ("yes", "no"):
            self._cache[k] = v
        return v


@dataclass
class BedrockJudge:
    """Real Bedrock judge. Constructed only when Eval D is enabled.

    Kept behind a thin wrapper so tests never import boto3.
    """

    model_id: str
    seed: int = 0
    region: str = "us-east-1"
    _client: object = None

    def _lazy_client(self) -> object:
        if self._client is None:
            import boto3  # noqa: PLC0415  (intentionally lazy)

            self._client = boto3.client("bedrock-runtime", region_name=self.region)
        return self._client

    def judge(self, candidate: str, reference: str) -> JudgeVerdict:  # pragma: no cover
        client = self._lazy_client()
        prompt = JUDGE_PROMPT_TEMPLATE.format(a=candidate, b=reference)
        response = client.converse(  # type: ignore[attr-defined]
            modelId=self.model_id,
            messages=[{"role": "user", "content": [{"text": prompt}]}],
            inferenceConfig={"temperature": 0.0, "topP": 1.0, "maxTokens": 64},
            additionalModelRequestFields={"seed": self.seed},
        )
        text = response["output"]["message"]["content"][0]["text"].strip().lower()
        first = text.split(None, 1)[0] if text else ""
        if first.startswith("yes"):
            return "yes"
        if first.startswith("no"):
            return "no"
        return "unknown"


def resolve_judge(
    *,
    enable_d: Optional[bool] = None,
    judge: Optional[Judge] = None,
    model_id: str = "anthropic.claude-3-haiku-20240307-v1:0",
    seed: int = 0,
) -> Judge:
    """Pick a judge. Tests inject `judge=StubJudge(...)`; CLI passes `enable_d`."""
    if judge is not None:
        return CachingJudge(inner=judge)
    if enable_d is None:
        enable_d = os.environ.get("PDFTOXL_ENABLE_D", "").lower() in {"1", "true", "yes"}
    if not enable_d:
        return CachingJudge(inner=DisabledJudge())
    return CachingJudge(inner=BedrockJudge(model_id=model_id, seed=seed))


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------


def _nfc(s: Optional[str]) -> str:
    return unicodedata.normalize("NFC", s) if s else ""


@dataclass
class CellResult:
    row_index: int
    candidate: str
    reference: str
    equivalent: bool
    method: Literal["exact", "ast", "judge", "judge-unknown", "skip"]


@dataclass
class MetricResult:
    score: float
    numerator: int
    denominator: int
    cells: list[CellResult] = field(default_factory=list)
    unresolved: int = 0  # number of cells that returned "unknown"


def branching_logic_equivalence(
    candidates: Sequence[Optional[str]],
    references: Sequence[Optional[str]],
) -> MetricResult:
    if len(candidates) != len(references):
        raise ValueError("candidates and references length mismatch")
    cells: list[CellResult] = []
    matched = 0
    considered = 0
    for idx, (c, r) in enumerate(zip(candidates, references)):
        c_txt = _nfc(c)
        r_txt = _nfc(r)
        if not c_txt and not r_txt:
            continue
        considered += 1
        try:
            eq = canonicalize(c_txt) == canonicalize(r_txt) if (c_txt and r_txt) else False
        except DSLParseError:
            eq = False
        if eq:
            matched += 1
        cells.append(
            CellResult(row_index=idx, candidate=c_txt, reference=r_txt, equivalent=eq, method="ast")
        )
    score = (matched / considered) if considered else 1.0
    return MetricResult(score=score, numerator=matched, denominator=considered, cells=cells)


def _text_equivalence(
    candidates: Sequence[Optional[str]],
    references: Sequence[Optional[str]],
    judge: Judge,
    preprocess=lambda s: s,
    compare=lambda a, b: a == b,
) -> MetricResult:
    if len(candidates) != len(references):
        raise ValueError("candidates and references length mismatch")
    cells: list[CellResult] = []
    matched = 0
    considered = 0
    unresolved = 0
    for idx, (c, r) in enumerate(zip(candidates, references)):
        c_txt = _nfc(c)
        r_txt = _nfc(r)
        if not c_txt and not r_txt:
            continue
        considered += 1
        cp = preprocess(c_txt)
        rp = preprocess(r_txt)
        if compare(cp, rp):
            matched += 1
            cells.append(
                CellResult(row_index=idx, candidate=c_txt, reference=r_txt, equivalent=True, method="exact")
            )
            continue
        verdict = judge.judge(c_txt, r_txt)
        if verdict == "yes":
            matched += 1
            cells.append(
                CellResult(row_index=idx, candidate=c_txt, reference=r_txt, equivalent=True, method="judge")
            )
        elif verdict == "no":
            cells.append(
                CellResult(row_index=idx, candidate=c_txt, reference=r_txt, equivalent=False, method="judge")
            )
        else:
            unresolved += 1
            cells.append(
                CellResult(
                    row_index=idx,
                    candidate=c_txt,
                    reference=r_txt,
                    equivalent=False,
                    method="judge-unknown",
                )
            )
    score = (matched / considered) if considered else 1.0
    return MetricResult(
        score=score, numerator=matched, denominator=considered, cells=cells, unresolved=unresolved
    )


def question_text_equivalence(
    candidates: Sequence[Optional[str]],
    references: Sequence[Optional[str]],
    judge: Judge,
) -> MetricResult:
    return _text_equivalence(candidates, references, judge)


def _split_checkbox(s: str) -> frozenset[str]:
    return frozenset(part.strip() for part in s.split("|") if part.strip())


def answer_text_equivalence(
    candidates: Sequence[Optional[str]],
    references: Sequence[Optional[str]],
    judge: Judge,
    *,
    is_checkbox: Optional[Sequence[bool]] = None,
) -> MetricResult:
    if is_checkbox is None:
        is_checkbox = [False] * len(candidates)
    if not (len(candidates) == len(references) == len(is_checkbox)):
        raise ValueError("inputs must be equal length")
    cells: list[CellResult] = []
    matched = 0
    considered = 0
    unresolved = 0
    for idx, (c, r, checkbox) in enumerate(zip(candidates, references, is_checkbox)):
        c_txt = _nfc(c)
        r_txt = _nfc(r)
        if not c_txt and not r_txt:
            continue
        considered += 1
        if checkbox:
            if _split_checkbox(c_txt) == _split_checkbox(r_txt):
                matched += 1
                cells.append(
                    CellResult(
                        row_index=idx, candidate=c_txt, reference=r_txt, equivalent=True, method="exact"
                    )
                )
                continue
        elif c_txt == r_txt:
            matched += 1
            cells.append(
                CellResult(row_index=idx, candidate=c_txt, reference=r_txt, equivalent=True, method="exact")
            )
            continue
        verdict = judge.judge(c_txt, r_txt)
        if verdict == "yes":
            matched += 1
            cells.append(
                CellResult(row_index=idx, candidate=c_txt, reference=r_txt, equivalent=True, method="judge")
            )
        elif verdict == "no":
            cells.append(
                CellResult(row_index=idx, candidate=c_txt, reference=r_txt, equivalent=False, method="judge")
            )
        else:
            unresolved += 1
            cells.append(
                CellResult(
                    row_index=idx,
                    candidate=c_txt,
                    reference=r_txt,
                    equivalent=False,
                    method="judge-unknown",
                )
            )
    score = (matched / considered) if considered else 1.0
    return MetricResult(
        score=score, numerator=matched, denominator=considered, cells=cells, unresolved=unresolved
    )


def equivalence_uplift_over_eval_B(
    per_column_equivalence: dict[str, float],
    per_column_accuracy_eval_b: dict[str, float],
) -> dict[str, float]:
    """Diagnostic: per-column (equivalence - eval_B_accuracy).

    Returns only columns present in `per_column_equivalence`; missing Eval B
    values default to 0.0 (worst case), which keeps the uplift conservative.
    """
    return {
        col: per_column_equivalence[col] - per_column_accuracy_eval_b.get(col, 0.0)
        for col in per_column_equivalence
    }


__all__ = [
    "BedrockJudge",
    "CachingJudge",
    "CellResult",
    "DisabledJudge",
    "Judge",
    "JudgeVerdict",
    "JUDGE_PROMPT_TEMPLATE",
    "JUDGE_PROMPT_VERSION",
    "MetricResult",
    "StubJudge",
    "answer_text_equivalence",
    "branching_logic_equivalence",
    "equivalence_uplift_over_eval_B",
    "question_text_equivalence",
    "resolve_judge",
]
