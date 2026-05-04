"""Conversation grouping + Q&A pairing.

The two corrections from the actual build are exercised here:
1. Subject-fallback `conversation_key` hashes on subject ALONE — mixing in
   `sender_domain` would prevent inbound (parent's domain) and outbound
   (stanford.edu) from ever colliding, so this test fails any regression
   that re-introduces the domain.
2. Multi-step back-and-forth threads yield multiple Q&A pairs; an outbound
   followed by another inbound starts a fresh pair.
"""

from __future__ import annotations

from sasi_mcp.store import MessageRecord
from sasi_mcp.thread import (
    _norm_subject,
    conversation_key,
    derive_qa_pairs,
    group_threads,
    summarize_threads,
)

MAILBOX = "summermed@stanford.edu"


def _msg(
    *,
    mid: str,
    received: str,
    subject: str,
    direction: str,
    sender: str,
    conversation_id: str = "",
    body: str = "",
) -> MessageRecord:
    return MessageRecord(
        message_id=mid,
        conversation_id=conversation_id,
        account=MAILBOX,
        folder="inbox" if direction == "inbound" else "sent",
        direction=direction,
        received_at=received,
        sender_email=sender,
        sender_name="",
        recipients=[],
        subject=subject,
        body_text=body,
    )


# ---- _norm_subject --------------------------------------------------------


def test_norm_subject_strips_re_fwd_chains():
    assert _norm_subject("Re: Fwd: RE: Question about deadline") == "question about deadline"
    assert _norm_subject("FW:   spaced  ") == "spaced"
    assert _norm_subject("Aw: AW: hallo") == "hallo"
    assert _norm_subject("") == ""


# ---- conversation_key — the subject-only invariant -----------------------


def test_conversation_key_uses_outlook_id_when_present():
    m = _msg(
        mid="1", received="2024-01-01T00:00:00Z", subject="X",
        direction="inbound", sender="parent@example.org", conversation_id="abc123",
    )
    assert conversation_key(m).startswith("cid:abc123")


def test_conversation_key_falls_back_to_subject_only_so_inbound_outbound_collide():
    """The critical regression test: inbound (parent's domain) and the matching
    outbound (stanford.edu) MUST land in the same bucket when the Outlook
    conversation_id is missing. Hashing on subject + sender_domain would
    prevent the pair from ever forming."""
    inbound = _msg(
        mid="q1", received="2024-01-01T09:00:00Z",
        subject="Re: registration question", direction="inbound",
        sender="parent@gmail.com",
    )
    outbound = _msg(
        mid="a1", received="2024-01-01T10:00:00Z",
        subject="Re: registration question", direction="outbound",
        sender=MAILBOX,
    )
    assert conversation_key(inbound) == conversation_key(outbound)


def test_conversation_key_separates_distinct_subjects():
    a = _msg(mid="a", received="2024-01-01T00:00:00Z", subject="deadline",
             direction="inbound", sender="x@example.com")
    b = _msg(mid="b", received="2024-01-01T00:00:00Z", subject="housing",
             direction="inbound", sender="x@example.com")
    assert conversation_key(a) != conversation_key(b)


def test_conversation_key_empty_subject_buckets_per_message():
    a = _msg(mid="a", received="2024-01-01T00:00:00Z", subject="",
             direction="inbound", sender="x@example.com")
    b = _msg(mid="b", received="2024-01-01T00:00:00Z", subject="",
             direction="inbound", sender="x@example.com")
    assert conversation_key(a) != conversation_key(b)


# ---- group_threads / summarize_threads -----------------------------------


def test_group_threads_sorts_by_received_at():
    msgs = [
        _msg(mid="2", received="2024-01-02T00:00:00Z", subject="x",
             direction="inbound", sender="a@b.com", conversation_id="t1"),
        _msg(mid="1", received="2024-01-01T00:00:00Z", subject="x",
             direction="inbound", sender="a@b.com", conversation_id="t1"),
    ]
    grouped = group_threads(msgs)
    [(_key, ms)] = grouped.items()
    assert [m.message_id for m in ms] == ["1", "2"]


def test_summarize_threads_marks_outbound_presence():
    msgs = [
        _msg(mid="1", received="2024-01-01T00:00:00Z", subject="x",
             direction="inbound", sender="parent@example.com",
             conversation_id="t1"),
        _msg(mid="2", received="2024-01-02T00:00:00Z", subject="x",
             direction="outbound", sender=MAILBOX, conversation_id="t1"),
    ]
    summaries = summarize_threads(group_threads(msgs))
    assert len(summaries) == 1
    assert summaries[0].has_outbound is True
    assert summaries[0].message_count == 2


# ---- derive_qa_pairs -----------------------------------------------------


def test_pair_skips_threads_with_no_outbound():
    msgs = [
        _msg(mid="1", received="2024-01-01T00:00:00Z", subject="never replied",
             direction="inbound", sender="parent@example.com",
             conversation_id="t1"),
    ]
    assert derive_qa_pairs(msgs) == []


def test_pair_skips_outbound_only_threads():
    msgs = [
        _msg(mid="2", received="2024-01-02T00:00:00Z", subject="we initiated",
             direction="outbound", sender=MAILBOX, conversation_id="t1"),
    ]
    assert derive_qa_pairs(msgs) == []


def test_pair_basic_inbound_then_outbound():
    msgs = [
        _msg(mid="1", received="2024-01-01T09:00:00Z", subject="deadline?",
             direction="inbound", sender="parent@example.com",
             conversation_id="t1"),
        _msg(mid="2", received="2024-01-01T10:00:00Z", subject="Re: deadline?",
             direction="outbound", sender=MAILBOX, conversation_id="t1"),
    ]
    pairs = derive_qa_pairs(msgs)
    assert len(pairs) == 1
    p = pairs[0]
    assert p.question_message_id == "1"
    assert p.answer_message_id == "2"
    assert p.review_status == "pending"
    assert p.received_at == "2024-01-01T10:00:00Z"


def test_pair_multi_step_thread_yields_multiple_pairs():
    """Inbound → outbound → inbound → outbound = two pairs (each fresh inbound
    starts a new pair after the prior outbound)."""
    msgs = [
        _msg(mid="q1", received="2024-01-01T09:00:00Z", subject="deadline?",
             direction="inbound", sender="parent@example.com",
             conversation_id="t1"),
        _msg(mid="a1", received="2024-01-01T10:00:00Z", subject="Re: deadline?",
             direction="outbound", sender=MAILBOX, conversation_id="t1"),
        _msg(mid="q2", received="2024-01-02T09:00:00Z", subject="Re: deadline?",
             direction="inbound", sender="parent@example.com",
             conversation_id="t1"),
        _msg(mid="a2", received="2024-01-02T10:00:00Z", subject="Re: deadline?",
             direction="outbound", sender=MAILBOX, conversation_id="t1"),
    ]
    pairs = derive_qa_pairs(msgs)
    assert [(p.question_message_id, p.answer_message_id) for p in pairs] == [
        ("q1", "a1"), ("q2", "a2"),
    ]


def test_pair_consecutive_inbounds_collapse_to_one_pair():
    """If a sender writes twice before we reply, the FIRST inbound pairs with
    the eventual outbound; the orphan second inbound is consumed and not
    re-paired."""
    msgs = [
        _msg(mid="q1", received="2024-01-01T09:00:00Z", subject="hi",
             direction="inbound", sender="parent@example.com",
             conversation_id="t1"),
        _msg(mid="q2", received="2024-01-01T09:30:00Z", subject="hi",
             direction="inbound", sender="parent@example.com",
             conversation_id="t1"),
        _msg(mid="a1", received="2024-01-01T10:00:00Z", subject="Re: hi",
             direction="outbound", sender=MAILBOX, conversation_id="t1"),
    ]
    pairs = derive_qa_pairs(msgs)
    assert len(pairs) == 1
    assert pairs[0].question_message_id == "q1"
    assert pairs[0].answer_message_id == "a1"


def test_pair_uses_subject_only_fallback_with_no_conversation_id():
    msgs = [
        _msg(mid="q1", received="2024-01-01T09:00:00Z",
             subject="Re: tuition question", direction="inbound",
             sender="parent@example.com"),
        _msg(mid="a1", received="2024-01-01T10:00:00Z",
             subject="Re: tuition question", direction="outbound",
             sender=MAILBOX),
    ]
    pairs = derive_qa_pairs(msgs)
    assert len(pairs) == 1
    assert pairs[0].question_message_id == "q1"
