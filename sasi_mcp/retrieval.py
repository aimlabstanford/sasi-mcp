"""Retrieval scoring: combine cosine, recency, and evergreen-boost.

Per the plan:

    final_score = cosine_similarity
                * recency_multiplier(received_at)
                * (1.0 + evergreen_boost * evergreen_score)
                * (0 if stale_reason and not user_overridden_stale else 1)

    recency_multiplier(t) = 0.7 + 0.3 * exp(-age_years(t) / half_life)
        # half_life default 3y → current=1.00, 1y=0.93, 3y=0.81, 5y=0.76

Stale pairs are excluded by default; `include_stale=True` returns them with
the `stale_reason` field set so callers can hedge appropriately.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timezone

from sasi_mcp.config import RetrievalConfig
from sasi_mcp.store import QAPair


@dataclass(slots=True)
class ScoredPair:
    qa: QAPair
    cosine: float
    recency: float
    evergreen_boost: float
    final_score: float


def _age_years(received_at: str, now: datetime | None = None) -> float:
    now = now or datetime.now(timezone.utc)
    try:
        dt = datetime.fromisoformat(received_at.replace("Z", "+00:00"))
    except ValueError:
        return 0.0
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    delta = now - dt
    return max(0.0, delta.days / 365.25)


def recency_multiplier(received_at: str, half_life_years: float, now: datetime | None = None) -> float:
    age = _age_years(received_at, now)
    return 0.7 + 0.3 * math.exp(-age / max(0.1, half_life_years))


def score_pair(
    qa: QAPair,
    cosine: float,
    cfg: RetrievalConfig,
    *,
    include_stale: bool = False,
    now: datetime | None = None,
) -> ScoredPair:
    rec = recency_multiplier(qa.received_at, cfg.recency_half_life_years, now)
    boost = 1.0 + cfg.evergreen_boost * float(qa.evergreen_score or 0.0)
    is_stale = bool(qa.stale_reason) and not bool(qa.user_overridden_stale)
    gate = 0.0 if (is_stale and not include_stale) else 1.0
    final = cosine * rec * boost * gate
    return ScoredPair(qa=qa, cosine=cosine, recency=rec, evergreen_boost=boost, final_score=final)
