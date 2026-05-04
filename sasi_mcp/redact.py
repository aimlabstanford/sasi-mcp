"""PII redaction. Local-only — no network calls.

Pipeline: regex pre-passes → Presidio analyzer → anonymizer with stable typed
placeholders. Per-pair numbering (no global identity tracking), so the same
PERSON in one body gets `<PERSON_1>`, `<PERSON_2>`, ... but those numbers do
not link across pairs.

Two regex pre-passes run BEFORE Presidio:
1. SUNet IDs adjacent to @stanford.edu → `<SUNET_n>` placeholders.
2. Dates carrying a year → `<DATE_YYYY>` so Presidio's DATE_TIME detector
   doesn't strip the year. The year is needed downstream for staleness
   audits, so it must survive redaction.

A custom Presidio recognizer adds Stanford 8-digit ID numbers (`0\\d{7}`).

Per-message redaction also writes one JSON line per message to
`redaction_audit.jsonl`: counts only, never the matched strings.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# --- regex pre-passes (run BEFORE Presidio) ---------------------------------

# SUNet (Stanford SUNet IDs are 3-8 lowercase alphanumeric, leading letter)
# anchored to @stanford.edu so we don't catch random short words.
_SUNET_RE = re.compile(r"\b([a-z][a-z0-9]{2,7})@stanford\.edu\b", re.IGNORECASE)

# Stanford 8-digit ID (begins with 0, total 8 digits).
_STANFORD_ID_RE = re.compile(r"\b0\d{7}\b")

# Dates with year. Several formats; we normalize them all to `<DATE_YYYY>`.
_MONTH = (
    r"(?:january|february|march|april|may|june|july|august|"
    r"september|october|november|december|"
    r"jan|feb|mar|apr|jun|jul|aug|sep|sept|oct|nov|dec)"
)
_DATE_PATTERNS: list[re.Pattern[str]] = [
    # "April 15, 2024" / "Apr 15 2024"
    re.compile(rf"\b{_MONTH}\s+\d{{1,2}}(?:,)?\s+(20\d\d|19\d\d)\b", re.IGNORECASE),
    # "15 April 2024"
    re.compile(rf"\b\d{{1,2}}\s+{_MONTH}\s+(20\d\d|19\d\d)\b", re.IGNORECASE),
    # "4/15/2024" or "4-15-2024"
    re.compile(r"\b\d{1,2}[/-]\d{1,2}[/-](20\d\d|19\d\d)\b"),
    # ISO "2024-04-15"
    re.compile(r"\b(20\d\d|19\d\d)-\d{2}-\d{2}\b"),
]


@dataclass(slots=True)
class RedactionResult:
    text: str
    counts: dict[str, int]


def _apply_sunet(text: str) -> tuple[str, int]:
    seen: dict[str, str] = {}

    def repl(m: re.Match[str]) -> str:
        sunet = m.group(1).lower()
        if sunet not in seen:
            seen[sunet] = f"<SUNET_{len(seen) + 1}>"
        return seen[sunet]

    new_text, count = _SUNET_RE.subn(repl, text)
    return new_text, count


def _apply_dates_with_year(text: str) -> tuple[str, int]:
    total = 0
    for pat in _DATE_PATTERNS:
        def repl(m: re.Match[str]) -> str:
            year = m.group(1)
            return f"<DATE_{year}>"

        text, n = pat.subn(repl, text)
        total += n
    return text, total


def _apply_stanford_id(text: str) -> tuple[str, int]:
    seen: dict[str, str] = {}

    def repl(m: re.Match[str]) -> str:
        sid = m.group(0)
        if sid not in seen:
            seen[sid] = f"<STANFORD_ID_{len(seen) + 1}>"
        return seen[sid]

    new_text, count = _STANFORD_ID_RE.subn(repl, text)
    return new_text, count


def regex_pre_pass(text: str) -> RedactionResult:
    """Run only the regex pre-passes. Useful for tests and as a fallback path
    when Presidio isn't installed in the local environment."""
    counts: Counter[str] = Counter()
    text, n = _apply_sunet(text)
    counts["SUNET"] += n
    text, n = _apply_dates_with_year(text)
    counts["DATE_WITH_YEAR"] += n
    text, n = _apply_stanford_id(text)
    counts["STANFORD_ID"] += n
    return RedactionResult(text=text, counts=dict(counts))


# --- Presidio path (lazy import; only required when actually redacting) -----


def _build_presidio_engine() -> tuple[Any, Any]:
    """Build the analyzer and anonymizer. Imports Presidio lazily."""
    from presidio_analyzer import AnalyzerEngine, PatternRecognizer  # type: ignore[import-not-found]
    from presidio_analyzer.nlp_engine import NlpEngineProvider  # type: ignore[import-not-found]
    from presidio_anonymizer import AnonymizerEngine  # type: ignore[import-not-found]

    provider = NlpEngineProvider(
        nlp_configuration={
            "nlp_engine_name": "spacy",
            "models": [{"lang_code": "en", "model_name": "en_core_web_lg"}],
        }
    )
    analyzer = AnalyzerEngine(nlp_engine=provider.create_engine(), supported_languages=["en"])

    # Custom recognizer for Stanford 8-digit IDs is also handled by the regex
    # pre-pass; we still register it with Presidio so anyone running analyzer
    # alone (e.g. via the API) gets the same coverage.
    stanford_id = PatternRecognizer(
        supported_entity="STANFORD_ID",
        patterns=[],
        deny_list=None,
        deny_list_score=1.0,
    )
    # Use a regex pattern via PatternRecognizer's .patterns list:
    from presidio_analyzer import Pattern  # type: ignore[import-not-found]

    stanford_id.patterns = [Pattern("stanford_id", r"\b0\d{7}\b", 0.85)]
    analyzer.registry.add_recognizer(stanford_id)

    return analyzer, AnonymizerEngine()


def redact_text(
    text: str,
    *,
    entities: list[str] | None = None,
    analyzer: Any | None = None,
    anonymizer: Any | None = None,
) -> RedactionResult:
    """Full redaction pipeline: regex pre-passes → Presidio.

    If Presidio is not installed, returns the regex-pre-pass result and
    records that fact in the counts under `_PRESIDIO_UNAVAILABLE` so the
    audit log makes it obvious.
    """
    pre = regex_pre_pass(text)
    counts = Counter(pre.counts)

    try:
        if analyzer is None or anonymizer is None:
            analyzer, anonymizer = _build_presidio_engine()
    except ImportError:
        counts["_PRESIDIO_UNAVAILABLE"] = 1
        return RedactionResult(text=pre.text, counts=dict(counts))

    ents = entities or [
        "PERSON", "EMAIL_ADDRESS", "PHONE_NUMBER", "US_SSN",
        "LOCATION", "CREDIT_CARD", "URL", "IP_ADDRESS", "STANFORD_ID",
    ]
    results = analyzer.analyze(text=pre.text, entities=ents, language="en")
    if not results:
        return RedactionResult(text=pre.text, counts=dict(counts))

    # Build per-pair stable numbering: the same surface form in this body
    # always gets the same number, but we never persist a global mapping.
    seen: dict[tuple[str, str], str] = {}

    # Sort by start position descending so replacements don't invalidate offsets.
    results_sorted = sorted(results, key=lambda r: r.start, reverse=True)
    out = pre.text
    for r in results_sorted:
        surface = out[r.start : r.end]
        key = (r.entity_type, surface.lower())
        if key not in seen:
            n = sum(1 for k in seen if k[0] == r.entity_type) + 1
            seen[key] = f"<{r.entity_type}_{n}>"
        placeholder = seen[key]
        out = out[: r.start] + placeholder + out[r.end :]
        counts[r.entity_type] += 1

    return RedactionResult(text=out, counts=dict(counts))


def append_audit(audit_path: Path, message_id: str, counts: dict[str, int]) -> None:
    """Write one line per redacted message: counts only, never matched text."""
    audit_path.parent.mkdir(parents=True, exist_ok=True)
    with audit_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps({"message_id": message_id, "counts": counts}) + "\n")
