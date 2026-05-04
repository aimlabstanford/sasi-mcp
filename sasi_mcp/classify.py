"""Classify each Q&A pair into one of the fixed taxonomy categories."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sasi_mcp.config import LLMConfig
from sasi_mcp.llm import complete_json
from sasi_mcp.logger import get_logger
from sasi_mcp.store import Store

_log = get_logger("sasi_mcp.classify")

CATEGORIES = [
    "finance_billing",
    "parent_logistics",
    "applicant_questions",
    "program_schedule",
    "housing_meals",
    "health_safety",
    "enrollment_status",
    "other",
]

_SYSTEM = (
    "Classify the redacted Q&A pair below into exactly ONE of these categories: "
    + ", ".join(CATEGORIES)
    + ". Respond with a single JSON object: "
    + '{"category": "<one_of>", "confidence": <0.0-1.0>}.'
)


@dataclass(slots=True)
class ClassifyResult:
    category: str
    confidence: float


def _coerce(d: dict[str, Any]) -> ClassifyResult:
    cat = str(d.get("category", "other")).strip().lower()
    if cat not in CATEGORIES:
        cat = "other"
    try:
        conf = float(d.get("confidence", 0.5))
    except (TypeError, ValueError):
        conf = 0.5
    return ClassifyResult(category=cat, confidence=max(0.0, min(1.0, conf)))


def classify_pair(question: str, answer: str, llm: LLMConfig) -> ClassifyResult:
    prompt = f"QUESTION:\n{question}\n\nANSWER:\n{answer}\n"
    raw = complete_json(
        provider=llm.provider,
        system=_SYSTEM,
        user=prompt,
        model=(llm.anthropic_model if llm.provider == "anthropic" else llm.model),
        ollama_url=llm.ollama_url,
    )
    return _coerce(raw)


def classify_all(store: Store, llm: LLMConfig) -> int:
    pending = [
        p for p in store.list_qa_pairs(status="pending")
        if p.category is None and p.canonical_question and p.canonical_answer
    ]
    n = 0
    for p in pending:
        try:
            result = classify_pair(p.canonical_question or "", p.canonical_answer or "", llm)
        except Exception as exc:
            _log.warning("classify.llm_failed", qa_id=p.qa_id, error=str(exc))
            continue
        store.update_qa_category(p.qa_id, result.category, result.confidence)
        n += 1
    return n
