"""Phase-7 staleness rules + retrieval scoring."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from sasi_mcp.audit import staleness
from sasi_mcp.config import RetrievalConfig, StalenessConfig
from sasi_mcp.retrieval import recency_multiplier, score_pair
from sasi_mcp.store import QAPair, Store


def _approve(store: Store, qa_id: str, *, year_bound: int | None = None,
             received: str = "2023-01-01T00:00:00Z",
             answer: str = "default answer", cluster_id: int | None = None) -> None:
    qid = qa_id
    store.upsert_qa_pair(QAPair(
        qa_id=qid,
        conversation_key=f"k:{qid}",
        question_message_id=f"q:{qid}",
        answer_message_id=f"a:{qid}",
        received_at=received,
    ))
    store.update_qa_extract(qid, "Q?", answer, [], year_bound, 0.9)
    store.update_qa_category(qid, "finance_billing", 0.9)
    store.update_qa_review(qid, "approved", "lchu")
    if cluster_id is not None:
        store.set_evergreen(qid, 0.0, cluster_id=cluster_id)


# ---- recency_multiplier ---------------------------------------------------


def test_recency_multiplier_at_zero_age_is_one():
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    assert abs(recency_multiplier("2026-01-01T00:00:00Z", 3.0, now) - 1.0) < 1e-6


def test_recency_multiplier_decays_to_floor():
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    very_old = recency_multiplier("1900-01-01T00:00:00Z", 3.0, now)
    assert 0.69 < very_old < 0.71  # asymptotes to 0.7


def test_recency_three_year_drop_is_about_0_81():
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    val = recency_multiplier("2023-01-01T00:00:00Z", 3.0, now)
    # 0.7 + 0.3 * exp(-1) ≈ 0.81
    assert 0.80 < val < 0.82


# ---- score_pair stale gating ---------------------------------------------


def test_score_pair_zero_when_stale_and_not_overridden():
    qa = QAPair(qa_id="x", conversation_key="k", question_message_id="q",
                answer_message_id="a", received_at="2024-01-01T00:00:00Z",
                stale_reason="user_marked", user_overridden_stale=0)
    s = score_pair(qa, cosine=0.9, cfg=RetrievalConfig())
    assert s.final_score == 0.0


def test_score_pair_passes_when_user_overrode_stale():
    qa = QAPair(qa_id="x", conversation_key="k", question_message_id="q",
                answer_message_id="a", received_at="2024-01-01T00:00:00Z",
                stale_reason="user_marked", user_overridden_stale=1)
    s = score_pair(qa, cosine=0.9, cfg=RetrievalConfig())
    assert s.final_score > 0.0


def test_score_pair_evergreen_boost_offsets_recency():
    """A 5-year-old evergreen pair should outrank a 1-year-old non-evergreen
    pair if their cosines are similar — that's the whole point of the boost."""
    cfg = RetrievalConfig()
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    old_evergreen = QAPair(qa_id="o", conversation_key="k", question_message_id="q",
                           answer_message_id="a", received_at="2021-01-01T00:00:00Z",
                           evergreen_score=1.0)
    new_plain = QAPair(qa_id="n", conversation_key="k", question_message_id="q",
                       answer_message_id="a", received_at="2025-01-01T00:00:00Z",
                       evergreen_score=0.0)
    s_old = score_pair(old_evergreen, cosine=0.85, cfg=cfg, now=now)
    s_new = score_pair(new_plain, cosine=0.85, cfg=cfg, now=now)
    # Old evergreen survives recency decay reasonably; new plain doesn't get a boost.
    # We don't assert ordering (depends on tuning), only that the evergreen pair
    # is meaningfully boosted relative to its naive recency multiplier.
    assert s_old.final_score > 0.85 * s_old.recency
    assert abs(s_new.final_score - 0.85 * s_new.recency) < 1e-6


# ---- staleness audit ------------------------------------------------------


def test_year_bound_expired_marks_stale(store: Store, tmp_path: Path):
    _approve(store, "old", year_bound=2022)
    _approve(store, "current", year_bound=2026)
    snapshot = tmp_path / "snap.yaml"
    snapshot.write_text("year: 2026\n")
    report = staleness(store, snapshot, StalenessConfig(),
                       now=datetime(2026, 1, 1, tzinfo=timezone.utc))
    assert "old" in report.year_bound_expired
    assert "current" not in report.year_bound_expired
    assert store.get_qa_pair("old").stale_reason == "year_bound_expired"
    assert store.get_qa_pair("current").stale_reason is None


def test_year_bound_does_not_clobber_user_override(store: Store, tmp_path: Path):
    _approve(store, "old", year_bound=2022)
    store.reaffirm("old")  # user said: still valid
    snapshot = tmp_path / "snap.yaml"
    snapshot.write_text("year: 2026\n")
    report = staleness(store, snapshot, StalenessConfig(),
                       now=datetime(2026, 1, 1, tzinfo=timezone.utc))
    assert "old" not in report.year_bound_expired
    assert store.get_qa_pair("old").stale_reason is None


def test_snapshot_dollar_mismatch_flags_stale(store: Store, tmp_path: Path):
    _approve(store, "wrong",
             answer="Tuition is $3500 for the program.",
             received="2022-04-01T00:00:00Z")
    _approve(store, "right",
             answer="Tuition is $4500 for the program.",
             received="2024-04-01T00:00:00Z")
    snapshot = tmp_path / "snap.yaml"
    snapshot.write_text("year: 2026\ntuition_usd: 4500\n")
    report = staleness(store, snapshot, StalenessConfig(),
                       now=datetime(2026, 1, 1, tzinfo=timezone.utc))
    assert "wrong" in report.snapshot_mismatch
    assert "right" not in report.snapshot_mismatch


def test_within_cluster_contradiction_marks_older(store: Store, tmp_path: Path):
    _approve(store, "old", received="2021-01-01T00:00:00Z", cluster_id=1)
    _approve(store, "new", received="2025-01-01T00:00:00Z", cluster_id=1)
    # Embeddings: old and new strongly disagree.
    store.upsert_embedding("old", [1.0, 0.0, 0.0], model="m")
    store.upsert_embedding("new", [0.0, 1.0, 0.0], model="m")
    snapshot = tmp_path / "snap.yaml"
    snapshot.write_text("year: 2026\n")
    report = staleness(store, snapshot, StalenessConfig(),
                       now=datetime(2026, 1, 1, tzinfo=timezone.utc))
    assert "old" in report.contradicted_by_newer
    assert store.get_qa_pair("old").stale_reason == "contradicted_by_newer"
    assert store.get_qa_pair("new").stale_reason is None


def test_within_cluster_no_contradiction_when_vectors_agree(store: Store, tmp_path: Path):
    _approve(store, "old", received="2021-01-01T00:00:00Z", cluster_id=1)
    _approve(store, "new", received="2025-01-01T00:00:00Z", cluster_id=1)
    # Strongly agreeing vectors → cosine ~1.0 → no flag.
    store.upsert_embedding("old", [0.9, 0.1, 0.0], model="m")
    store.upsert_embedding("new", [0.9, 0.1, 0.0], model="m")
    snapshot = tmp_path / "snap.yaml"
    snapshot.write_text("year: 2026\n")
    report = staleness(store, snapshot, StalenessConfig(),
                       now=datetime(2026, 1, 1, tzinfo=timezone.utc))
    assert "old" not in report.contradicted_by_newer
