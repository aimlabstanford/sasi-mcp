"""Pair-quality filter unit tests — covers the failure modes seen in the
May 2026 audit: ack-only pairs, Q≈A duplication, and missing question signals."""

from __future__ import annotations

from sasi_mcp.quality import is_quality_pair


def test_clean_question_passes():
    ok, reason = is_quality_pair(
        "How can I get an application fee waiver?",
        "Application fee waivers may be available through Regular Decision. "
        "Once you begin your application I can send you the form.",
    )
    assert ok, reason


def test_short_question_rejected():
    ok, reason = is_quality_pair("Yes thanks", "We have received your forms.")
    assert not ok
    assert reason == "q_too_short"


def test_short_answer_rejected():
    ok, reason = is_quality_pair("How do I submit my recommenders?", "By email.")
    assert not ok
    assert reason == "a_too_short"


def test_acknowledgment_question_rejected():
    ok, reason = is_quality_pair(
        "Thanks!",
        "You're welcome please let us know if you need anything else.",
    )
    assert not ok
    assert reason in {"q_too_short", "q_is_ack"}


def test_acknowledgment_answer_rejected():
    ok, reason = is_quality_pair(
        "Did you receive my transcript?",
        "Received",
    )
    assert not ok
    assert reason in {"a_too_short", "a_is_ack"}


def test_no_question_signal_rejected():
    # The d706c4dc / 01338ee9 cases: parent says "I uploaded my transcript", staff
    # acknowledges. Q is a statement, not a question.
    ok, reason = is_quality_pair(
        "I uploaded my transcript on the portal yesterday morning.",
        "Thank you for your quick response. I have received your transcript "
        "and have noted that the application allowed only one attachment.",
    )
    assert not ok
    assert reason == "no_question_signal"


def test_question_with_question_mark_passes():
    ok, _ = is_quality_pair(
        "Submitted application earlier today?",
        "Yes I can confirm we received your application this morning at 10am.",
    )
    assert ok


def test_question_with_interrogative_lead_passes():
    ok, _ = is_quality_pair(
        "Can you tell me when the medical forms are due",
        "Medical forms are due two weeks before the program start date.",
    )
    assert ok


def test_q_equals_a_rejected():
    text = "We have received your signed forms thank you so much for sending them"
    ok, reason = is_quality_pair(text, text)
    # "We have" doesn't match question-signal patterns, so the no_question_signal
    # rule fires before q_equals_a; either way the pair is rejected.
    assert not ok
    assert reason in {"no_question_signal", "q_equals_a"}


def test_q_subset_of_a_rejected():
    # Mirrors the d706c4dc pattern: A is a longer version of Q (or vice versa)
    # because extract pulled the same outbound block twice.
    q = "Hello you signed the forms we needed on your onboarding link below we have the ePOM form on file thank you"
    a = ("Hello you signed the forms we needed on your onboarding link below we "
         "have the ePOM form on file thank you Please let me know if I can assist "
         "you with anything else.")
    ok, reason = is_quality_pair(q, a)
    assert not ok
    assert reason in {"no_question_signal", "q_subset_a"}
