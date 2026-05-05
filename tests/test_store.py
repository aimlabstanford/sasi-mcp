"""SQLite schema + DAO behavior."""

from __future__ import annotations

from sasi_mcp.store import MessageRecord, QAPair, Store, pack_vector, unpack_vector


def _msg(message_id: str = "m1", direction: str = "inbound", body: str = "raw body",
         redacted: int = 0) -> MessageRecord:
    return MessageRecord(
        message_id=message_id,
        conversation_id="t1",
        account="summermed@stanford.edu",
        folder="inbox" if direction == "inbound" else "sent",
        direction=direction,
        received_at="2024-01-01T00:00:00Z",
        sender_email="parent@example.com" if direction == "inbound" else "summermed@stanford.edu",
        sender_name="P",
        recipients=[{"name": "S", "email": "summermed@stanford.edu"}],
        subject="hi",
        body_text=body,
        body_redacted=redacted,
    )


def _qa(qa_id: str = "qa1") -> QAPair:
    return QAPair(
        qa_id=qa_id,
        conversation_key="cid:t1",
        question_message_id="m1",
        answer_message_id="m2",
        received_at="2024-01-01T01:00:00Z",
    )


def test_pack_unpack_vector_roundtrip():
    v = [0.1, -0.5, 0.7]
    blob = pack_vector(v)
    assert isinstance(blob, bytes)
    out = unpack_vector(blob)
    assert len(out) == 3
    for a, b in zip(v, out):
        assert abs(a - b) < 1e-6


def test_upsert_message_inserts_then_updates(store: Store):
    assert store.upsert_message(_msg(message_id="m1")) is True
    # Same message id: returns False (update path).
    assert store.upsert_message(_msg(message_id="m1", body="changed")) is False
    rows = store.list_messages()
    assert len(rows) == 1
    assert rows[0].body_text == "changed"


def test_redacted_body_not_overwritten_on_reingest(store: Store):
    """Once redacted, re-ingesting the raw body must NOT replace the redacted
    text. This prevents the cron from leaking raw PII back into the db."""
    store.upsert_message(_msg(message_id="m1", body="raw with PII", redacted=0))
    store.update_redacted_body("m1", "redacted version")

    # Simulate a re-ingest attempt with the original raw body.
    store.upsert_message(_msg(message_id="m1", body="raw with PII", redacted=0))
    rows = store.list_messages()
    assert rows[0].body_text == "redacted version"
    assert rows[0].body_redacted == 1


def test_list_unredacted_filters_by_flag(store: Store):
    store.upsert_message(_msg(message_id="m1", body="a", redacted=0))
    store.upsert_message(_msg(message_id="m2", body="b", redacted=0))
    store.update_redacted_body("m1", "REDACTED")
    rows = store.list_unredacted()
    ids = sorted(r.message_id for r in rows)
    assert ids == ["m2"]


def test_qa_pair_unique_on_question_answer(store: Store):
    qa = _qa("qa1")
    assert store.upsert_qa_pair(qa) is True
    # Same question + answer ids → should fail uniqueness, return False.
    qa_dup = _qa("qa2")  # different qa_id, same q/a message ids
    assert store.upsert_qa_pair(qa_dup) is False
    assert len(store.list_qa_pairs()) == 1


def test_qa_review_lifecycle(store: Store):
    store.upsert_qa_pair(_qa("qa1"))
    store.update_qa_extract("qa1", "Q?", "A.", ["2024"], 2024, 0.9)
    store.update_qa_category("qa1", "program_schedule", 0.8)
    store.update_qa_review("qa1", "approved", "lchu")
    qa = store.get_qa_pair("qa1")
    assert qa is not None
    assert qa.review_status == "approved"
    assert qa.canonical_question == "Q?"
    assert qa.year_bound == 2024
    assert qa.category == "program_schedule"


def test_search_filters_excludes_pending(store: Store):
    store.upsert_qa_pair(_qa("qa1"))
    store.update_qa_review("qa1", "approved", "lchu")
    store.upsert_qa_pair(QAPair(
        qa_id="qa2", conversation_key="cid:t2",
        question_message_id="m3", answer_message_id="m4",
        received_at="2024-02-01T00:00:00Z",
    ))
    approved = store.list_qa_pairs(status="approved")
    pending = store.list_qa_pairs(status="pending")
    assert {p.qa_id for p in approved} == {"qa1"}
    assert {p.qa_id for p in pending} == {"qa2"}


def test_mark_stale_and_reaffirm(store: Store):
    store.upsert_qa_pair(_qa("qa1"))
    store.update_qa_review("qa1", "approved", "lchu")
    store.mark_stale("qa1", "user_marked")
    assert store.get_qa_pair("qa1").stale_reason == "user_marked"
    store.reaffirm("qa1")
    qa = store.get_qa_pair("qa1")
    assert qa.stale_reason is None
    assert qa.user_overridden_stale == 1


def test_supersede_links_pairs_and_marks_old_stale(store: Store):
    store.upsert_qa_pair(_qa("old"))
    store.upsert_qa_pair(QAPair(
        qa_id="new", conversation_key="cid:t2",
        question_message_id="m3", answer_message_id="m4",
        received_at="2025-01-01T00:00:00Z",
    ))
    store.supersede("old", "new")
    old = store.get_qa_pair("old")
    new = store.get_qa_pair("new")
    assert old.stale_reason == "superseded"
    assert new.supersedes_qa_id == "old"


def test_include_stale_filter(store: Store):
    store.upsert_qa_pair(_qa("qa1"))
    store.update_qa_review("qa1", "approved", "lchu")
    store.mark_stale("qa1", "user_marked")
    visible = store.list_qa_pairs(status="approved", include_stale=False)
    assert visible == []  # excluded by default
    visible_all = store.list_qa_pairs(status="approved", include_stale=True)
    assert {p.qa_id for p in visible_all} == {"qa1"}


def test_embedding_upsert_replaces(store: Store):
    store.upsert_qa_pair(_qa("qa1"))
    store.upsert_embedding("qa1", [0.1, 0.2, 0.3], model="m")
    store.upsert_embedding("qa1", [0.4, 0.5, 0.6], model="m")
    rows = store.list_embeddings()
    assert len(rows) == 1
    qa_id, vec, model = rows[0]
    assert qa_id == "qa1"
    assert abs(vec[0] - 0.4) < 1e-6
    assert model == "m"


def test_query_log_round_trip(store: Store):
    qid = store.log_query("how do I apply?", ["qa1", "qa2"], 0.91)
    rows = store.list_recent_queries(limit=5)
    assert any(r["query_id"] == qid for r in rows)
    store.flag_query(qid, "wrong")
    rows = store.list_recent_queries(limit=5)
    flagged = next(r for r in rows if r["query_id"] == qid)
    assert flagged["flagged_reason"] == "wrong"


def test_meta_get_set(store: Store):
    assert store.get_meta("nope") is None
    store.set_meta("k", "v")
    assert store.get_meta("k") == "v"
    store.set_meta("k", "v2")
    assert store.get_meta("k") == "v2"


def test_stats_shape(store: Store):
    store.upsert_message(_msg(message_id="m1"))
    store.upsert_qa_pair(_qa("qa1"))
    s = store.stats()
    assert s["messages"] == 1
    assert s["qa_pairs"] == 1
    assert s["qa_approved"] == 0
    assert s["qa_pending"] == 1
