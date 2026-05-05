"""Canonicalize Q&A pairs.

For each pending pair, ask the LLM to:
* `canonical_question` — clean self-contained question (greetings/sign-offs
  stripped, redaction tokens preserved verbatim).
* `canonical_answer` — substantive reply, no greeting/footer/quoted question.
* `temporal_anchors` — list of years, dollar amounts, durations, cohort refs.
* `year_bound` — 4-digit year if the answer is explicitly tied to one cohort
  year, else null.
* `confidence` — 0.0-1.0 self-rated faithfulness.

The Phase-3 redactor already replaced dates-with-year with `<DATE_YYYY>`, so
year extraction here also reads back from those tokens as a regex fallback
when the LLM omits them.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from sasi_mcp.config import LLMConfig
from sasi_mcp.llm import complete_json
from sasi_mcp.logger import get_logger
from sasi_mcp.store import MessageRecord, QAPair, Store

_log = get_logger("sasi_mcp.extract")

_SYSTEM = (
    "You are a careful summarizer. Given the redacted text of an inbound "
    "question and an outbound reply from a Stanford summer-program coordinator's "
    "shared mailbox, produce a JSON object with these keys:\n"
    "  canonical_question: a single clean self-contained question. Strip "
    "greetings, sign-offs, and quoted prior context. Preserve redaction "
    "placeholders like <PERSON_1>, <DATE_2024> verbatim.\n"
    "  canonical_answer: the substantive reply, with greetings/footers/quoted "
    "question stripped. Preserve redaction placeholders verbatim.\n"
    "  temporal_anchors: array of strings — years (e.g. \"2023\"), dollar "
    "amounts (e.g. \"$4500\"), durations, cohort references, deadlines.\n"
    "  year_bound: a 4-digit year if the answer is explicitly tied to one "
    "specific cohort/year, else null.\n"
    "  confidence: a float 0.0-1.0 expressing self-rated faithfulness.\n"
    "Return ONLY the JSON object."
)


_RE_DATE_YEAR_TOKEN = re.compile(r"<DATE_(\d{4})>")
_RE_BARE_YEAR = re.compile(r"\b(20\d\d|19\d\d)\b")
_RE_DOLLAR = re.compile(r"\$[\d,]+(?:\.\d+)?")
_RE_COHORT = re.compile(r"\b(20\d\d)\s+cohort\b", re.IGNORECASE)
_RE_DURING_YEAR = re.compile(r"\b(?:for|during|in)\s+the\s+(20\d\d)\b", re.IGNORECASE)


@dataclass(slots=True)
class ExtractResult:
    canonical_question: str
    canonical_answer: str
    temporal_anchors: list[str]
    year_bound: int | None
    confidence: float


def _regex_temporal_anchors(text: str) -> list[str]:
    out: list[str] = []
    out.extend(set(_RE_DATE_YEAR_TOKEN.findall(text)))
    out.extend(set(_RE_BARE_YEAR.findall(text)) - set(out))
    out.extend(set(_RE_DOLLAR.findall(text)))
    return out


def _regex_year_bound(text: str) -> int | None:
    m = _RE_COHORT.search(text)
    if m:
        return int(m.group(1))
    m = _RE_DURING_YEAR.search(text)
    if m:
        return int(m.group(1))
    return None


def _coerce_extract(d: dict[str, Any], answer_text: str) -> ExtractResult:
    cq = str(d.get("canonical_question", "") or "").strip()
    ca = str(d.get("canonical_answer", "") or "").strip()
    anchors = d.get("temporal_anchors") or []
    if not isinstance(anchors, list):
        anchors = [str(anchors)]
    anchors = [str(a) for a in anchors]
    if not anchors:
        anchors = _regex_temporal_anchors(answer_text)
    yb = d.get("year_bound")
    yb_int: int | None
    try:
        yb_int = int(yb) if yb is not None else None
    except (TypeError, ValueError):
        yb_int = None
    if yb_int is None:
        yb_int = _regex_year_bound(answer_text)
    try:
        conf = float(d.get("confidence", 0.5))
    except (TypeError, ValueError):
        conf = 0.5
    conf = max(0.0, min(1.0, conf))
    return ExtractResult(
        canonical_question=cq,
        canonical_answer=ca,
        temporal_anchors=anchors,
        year_bound=yb_int,
        confidence=conf,
    )


def extract_pair(
    question: MessageRecord,
    answer: MessageRecord,
    llm: LLMConfig,
) -> ExtractResult:
    """Run the LLM on one pair. Caller is responsible for ensuring both bodies
    are already redacted."""
    prompt = (
        f"INBOUND_SUBJECT: {question.subject}\n"
        f"INBOUND_BODY:\n{question.body_text}\n\n"
        f"OUTBOUND_BODY:\n{answer.body_text}\n"
    )
    raw = complete_json(
        provider=llm.provider,
        system=_SYSTEM,
        user=prompt,
        model=(llm.anthropic_model if llm.provider == "anthropic" else llm.model),
        ollama_url=llm.ollama_url,
    )
    return _coerce_extract(raw, answer.body_text)


def extract_all(store: Store, llm: LLMConfig) -> int:
    """Run extraction over every pair lacking a canonical_question. Returns the
    number of pairs newly populated."""
    pending = [p for p in store.list_qa_pairs(status="pending")
               if not p.canonical_question]
    if not pending:
        return 0
    msg_index = {m.message_id: m for m in store.list_messages()}
    n = 0
    for pair in pending:
        q = msg_index.get(pair.question_message_id)
        a = msg_index.get(pair.answer_message_id)
        if q is None or a is None:
            continue
        if q.body_redacted == 0 or a.body_redacted == 0:
            _log.warning("extract.skip_unredacted", qa_id=pair.qa_id)
            continue
        try:
            result = extract_pair(q, a, llm)
        except Exception as exc:
            _log.warning("extract.llm_failed", qa_id=pair.qa_id, error=str(exc))
            continue
        store.update_qa_extract(
            pair.qa_id,
            canonical_question=result.canonical_question,
            canonical_answer=result.canonical_answer,
            temporal_anchors=result.temporal_anchors,
            year_bound=result.year_bound,
            confidence=result.confidence,
        )
        n += 1
    return n
