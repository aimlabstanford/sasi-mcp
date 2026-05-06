"""Pair-quality filter — cheap heuristics that catch the most common extract failures
before a pair gets auto-approved or trusted by retrieval.

Failure modes observed in the May 2026 quality audit (8-pair sample, ~50% bad):
  - Acknowledgment threads ("signed forms, thanks") — no question, just chatter.
  - Q ≈ A — extract picked the same outbound block twice, so question and answer are
    near-identical staff text rather than parent-question / staff-answer.
  - Cross-thread bleed — Q from one parent's email, A from a different thread's
    response; conversation_key fix helped but templated subjects still slip through.

The cheap rules below catch (1) and (2) reliably; (3) needs an LLM coherence check
or per-category clustering and is left to the audit pipeline.
"""

from __future__ import annotations

import re

# Words/phrases that signal the speaker is asking, not stating. Order doesn't matter;
# we match anywhere in the question after a `?` short-circuit.
_QUESTION_LEADERS = (
    "what", "how", "when", "where", "why", "who", "which",
    "can", "could", "would", "should", "do", "does", "did",
    "is", "are", "was", "were", "will", "may", "might",
    "may i", "should i", "could you", "can you", "could we", "can we",
    "is there", "are there", "do i", "do we", "any chance",
    "let me know", "please advise", "wondering if", "wondering whether",
    "any way", "do you have", "is it", "are you",
)

# Acknowledgment / closing phrases that show up as one-line "answers" or "questions"
# in the corpus; if the entire body of a pair is one of these, it's not real Q&A.
_ACK_PATTERNS = (
    re.compile(r"^\s*(thanks?|thank you|got it|received|noted|confirmed|ok(?:ay)?|sounds good)[\s!.,]*$", re.IGNORECASE),
    re.compile(r"^\s*(received|signed|submitted|done|all set)[\s!.,]*$", re.IGNORECASE),
)

_WORD_RE = re.compile(r"\w+")


def _word_count(text: str) -> int:
    return len(_WORD_RE.findall(text or ""))


def _has_question_signal(q: str) -> bool:
    if not q:
        return False
    if "?" in q:
        return True
    lead = q.strip().lower()
    for marker in _QUESTION_LEADERS:
        if lead.startswith(marker + " ") or f" {marker} " in lead:
            return True
    return False


def _is_acknowledgment(text: str) -> bool:
    return any(p.match(text or "") for p in _ACK_PATTERNS)


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip().lower())


def is_quality_pair(question: str | None, answer: str | None) -> tuple[bool, str]:
    """Return (ok, reason). `reason` names the rule that fired when ok=False
    so audit logs can summarize failure modes."""
    q = question or ""
    a = answer or ""

    if _word_count(q) < 4:
        return False, "q_too_short"
    if _word_count(a) < 6:
        return False, "a_too_short"

    if _is_acknowledgment(q):
        return False, "q_is_ack"
    if _is_acknowledgment(a):
        return False, "a_is_ack"

    if not _has_question_signal(q):
        return False, "no_question_signal"

    qn, an = _normalize(q), _normalize(a)
    if qn == an:
        return False, "q_equals_a"
    # Catch the d706c4dc pattern where Q and A are the same outbound text
    # with one prefix word changed: substring containment + length parity.
    if len(qn) > 30 and len(an) > 30:
        if qn in an or an in qn:
            return False, "q_subset_a"

    return True, "ok"
