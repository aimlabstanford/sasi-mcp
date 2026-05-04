"""Regex pre-passes that run BEFORE Presidio.

The crucial property is that dates with year survive redaction as `<DATE_YYYY>`
tokens — Presidio's DATE_TIME detector strips the year by default, but the
year is needed downstream for the staleness audit.
"""

from __future__ import annotations

from sasi_mcp.redact import regex_pre_pass


def test_sunet_replaced_with_stable_placeholder():
    text = "ping abc123@stanford.edu — and again abc123@stanford.edu"
    out = regex_pre_pass(text)
    assert "abc123@stanford.edu" not in out.text
    # Same SUNet → same number within a body.
    assert out.text.count("<SUNET_1>") == 2
    assert out.counts["SUNET"] == 2


def test_sunet_distinct_ids_get_distinct_numbers():
    text = "abc123@stanford.edu and xyz999@stanford.edu"
    out = regex_pre_pass(text)
    assert "<SUNET_1>" in out.text
    assert "<SUNET_2>" in out.text


def test_iso_date_year_preserved():
    out = regex_pre_pass("registration closes 2024-04-15")
    assert "<DATE_2024>" in out.text
    assert "2024-04-15" not in out.text


def test_long_form_date_year_preserved():
    out = regex_pre_pass("see you on April 15, 2024 for orientation")
    assert "<DATE_2024>" in out.text


def test_short_form_date_year_preserved():
    out = regex_pre_pass("the deadline was 4/15/2023")
    assert "<DATE_2023>" in out.text


def test_european_date_year_preserved():
    out = regex_pre_pass("starts 22 June 2026")
    assert "<DATE_2026>" in out.text


def test_stanford_id_replaced():
    out = regex_pre_pass("their SUID is 06012345 in the records")
    assert "06012345" not in out.text
    assert "<STANFORD_ID_1>" in out.text


def test_audit_counts_no_strings_only_counts():
    """Sanity: the RedactionResult.counts is a dict[str, int] — never carries
    matched strings. The audit log writer asserts this property by serializing
    through json with default=None on extras."""
    out = regex_pre_pass("abc123@stanford.edu meets on 2024-04-15")
    assert all(isinstance(k, str) and isinstance(v, int) for k, v in out.counts.items())
