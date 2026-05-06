"""HTTP transport tests.

Privacy-critical contract — the test cases enforce the redacted-only rule:
no raw subjects, sender_email, sender_name, message_id, or conversation_key
appear in any response. Bodies are returned only when body_redacted=1.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from sasi_mcp.config import Config, OutlookConfig
from sasi_mcp.http_server import (
    create_app,
    make_thread_id,
    resolve_thread_id,
)
from sasi_mcp.store import MessageRecord, Store

_TOKEN = "test-bearer-token"
_SECRET = b"unit-test-hmac-secret"


# ---- fixtures -----------------------------------------------------------

def _make_config(db_path: Path) -> Config:
    """Minimal Config that points the store at db_path. Bypasses YAML loading."""
    return Config(
        data_dir=db_path.parent,
        outlook=OutlookConfig(mailbox_email="summermed@stanford.edu",
                              folders=["inbox", "sent"], days_back=365),
    )


def _seed_threads(db_path: Path) -> None:
    """Three threads:
      - parent_a: 2 messages (inbound, outbound) — has reply, NOT needs_reply
      - parent_b: 1 message (inbound, redacted) — needs_reply
      - parent_c: 1 message (inbound, raw) — needs_reply but unredacted
    """
    s = Store(db_path)
    common = dict(
        account="summermed@stanford.edu",
        recipients=[{"name": "S", "email": "summermed@stanford.edu"}],
    )
    s.upsert_message(MessageRecord(
        message_id="m_a1", conversation_id="cv_a", folder="inbox",
        direction="inbound", received_at="2026-04-10T10:00:00Z",
        sender_email="parent_a@example.com", sender_name="Parent A",
        subject="Medical forms question",
        body_text="Hello, my child <PERSON_1> needs the medical forms.",
        body_redacted=1, **common,
    ))
    s.upsert_message(MessageRecord(
        message_id="m_a2", conversation_id="cv_a", folder="sent",
        direction="outbound", received_at="2026-04-11T09:00:00Z",
        sender_email="summermed@stanford.edu", sender_name="SASI Staff",
        subject="Re: Medical forms question",
        recipients=[{"name": "Parent A", "email": "parent_a@example.com"}],
        body_text="Forms are due two weeks before program start.",
        body_redacted=1, account="summermed@stanford.edu",
    ))
    s.upsert_message(MessageRecord(
        message_id="m_b1", conversation_id="cv_b", folder="inbox",
        direction="inbound", received_at="2026-05-01T14:00:00Z",
        sender_email="parent_b@example.com", sender_name="Parent B",
        subject="Housing question",
        body_text="When does dorm check-in start for <PERSON_2>?",
        body_redacted=1, **common,
    ))
    s.upsert_message(MessageRecord(
        message_id="m_c1", conversation_id="cv_c", folder="inbox",
        direction="inbound", received_at="2026-05-05T08:00:00Z",
        sender_email="parent_c@example.com", sender_name="Parent C",
        subject="Application status",
        body_text="My SSN is 123-45-6789, what's my application status?",
        body_redacted=0, **common,
    ))
    s.close()


@pytest.fixture()
def client(tmp_path: Path) -> Iterator[TestClient]:
    db_path = tmp_path / "corpus.sqlite"
    _seed_threads(db_path)
    cfg = _make_config(db_path)
    app = create_app(cfg, token=_TOKEN, secret=_SECRET)
    with TestClient(app) as c:
        yield c


def _auth() -> dict[str, str]:
    return {"Authorization": f"Bearer {_TOKEN}"}


# ---- thread_id helpers --------------------------------------------------

def test_make_thread_id_deterministic():
    a = make_thread_id(_SECRET, "cid:cv_b:parent_b@example.com")
    b = make_thread_id(_SECRET, "cid:cv_b:parent_b@example.com")
    assert a == b
    assert len(a) == 16


def test_make_thread_id_changes_with_secret():
    k = "cid:cv_b:parent_b@example.com"
    assert make_thread_id(b"secret-1", k) != make_thread_id(b"secret-2", k)


def test_resolve_thread_id_roundtrip():
    keys = ["cid:cv_a:p@x.com", "cid:cv_b:q@y.com"]
    tid = make_thread_id(_SECRET, keys[1])
    resolved = resolve_thread_id(_SECRET, tid, keys)
    assert resolved == keys[1]


def test_resolve_thread_id_unknown_returns_none():
    keys = ["cid:cv_a:p@x.com"]
    bogus = "0" * 16
    assert resolve_thread_id(_SECRET, bogus, keys) is None


# ---- healthz ------------------------------------------------------------

def test_healthz_no_auth_required(client: TestClient):
    r = client.get("/v1/healthz")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert "schema_version" in body
    assert "embeddings" in body


# ---- bearer auth --------------------------------------------------------

def test_threads_requires_bearer(client: TestClient):
    r = client.get("/v1/threads")
    assert r.status_code == 401


def test_threads_rejects_wrong_token(client: TestClient):
    r = client.get("/v1/threads", headers={"Authorization": "Bearer wrong"})
    assert r.status_code == 401


def test_search_requires_bearer(client: TestClient):
    r = client.post("/v1/search", json={"question": "x"})
    assert r.status_code == 401


# ---- threads list -------------------------------------------------------

def test_threads_list_default_status_is_needs_reply(client: TestClient):
    r = client.get("/v1/threads", headers=_auth())
    assert r.status_code == 200
    body = r.json()
    threads = body["threads"]
    # parent_b (redacted, latest inbound) and parent_c (unredacted, latest
    # inbound) should be in needs_reply. parent_a (latest outbound) should not.
    directions = [t["latest_direction"] for t in threads]
    assert all(d == "inbound" for d in directions)
    assert len(threads) == 2


def test_threads_list_status_all_includes_replied(client: TestClient):
    r = client.get("/v1/threads?status=all", headers=_auth())
    assert r.status_code == 200
    threads = r.json()["threads"]
    assert len(threads) == 3
    # Sorted descending by last_received_at
    timestamps = [t["last_received_at"] for t in threads]
    assert timestamps == sorted(timestamps, reverse=True)


def test_threads_list_payload_omits_raw_pii_fields(client: TestClient):
    r = client.get("/v1/threads?status=all", headers=_auth())
    body = r.text
    # None of these should appear in any thread payload — they're all raw.
    assert "parent_a@example.com" not in body
    assert "parent_b@example.com" not in body
    assert "parent_c@example.com" not in body
    assert "Parent A" not in body
    assert "Medical forms question" not in body  # raw subject
    assert "cid:cv_a" not in body  # raw conversation_key
    assert "m_a1" not in body  # raw message_id
    # Allowed fields:
    threads = r.json()["threads"]
    for t in threads:
        assert set(t.keys()) == {
            "thread_id", "first_received_at", "last_received_at",
            "message_count", "has_outbound", "latest_direction",
            "redacted_preview",
        }


def test_threads_list_redacted_preview_only_when_redacted(client: TestClient):
    r = client.get("/v1/threads?status=all", headers=_auth())
    threads = r.json()["threads"]
    # Find the parent_c thread (unredacted body) — preview must be empty
    by_ts = {t["last_received_at"]: t for t in threads}
    parent_c = by_ts["2026-05-05T08:00:00Z"]
    assert parent_c["redacted_preview"] == ""
    # parent_b has redacted body — preview should be non-empty
    parent_b = by_ts["2026-05-01T14:00:00Z"]
    assert parent_b["redacted_preview"] != ""
    assert "<PERSON_2>" in parent_b["redacted_preview"]


def test_threads_list_pending_redaction_count(client: TestClient):
    r = client.get("/v1/threads", headers=_auth())
    body = r.json()
    # parent_c is needs_reply with no redaction → counts as pending.
    # parent_b is needs_reply with redaction → does not count.
    assert body["pending_redaction_count"] == 1


def test_threads_list_limit_enforced(client: TestClient):
    r = client.get("/v1/threads?status=all&limit=1", headers=_auth())
    body = r.json()
    assert len(body["threads"]) == 1
    assert body["total_returned"] == 1


def test_threads_list_invalid_status(client: TestClient):
    r = client.get("/v1/threads?status=replied", headers=_auth())
    assert r.status_code == 400


# ---- thread detail ------------------------------------------------------

def _resolve_thread_id_from_list(client: TestClient, latest_ts: str) -> str:
    r = client.get("/v1/threads?status=all", headers=_auth())
    by_ts = {t["last_received_at"]: t for t in r.json()["threads"]}
    return by_ts[latest_ts]["thread_id"]


def test_thread_detail_redacted_body_only(client: TestClient):
    tid = _resolve_thread_id_from_list(client, "2026-05-01T14:00:00Z")  # parent_b
    r = client.get(f"/v1/threads/{tid}", headers=_auth())
    assert r.status_code == 200
    body = r.json()
    assert body["thread_id"] == tid
    assert body["unredacted_message_count"] == 0
    msg = body["messages"][0]
    assert set(msg.keys()) == {
        "message_index", "direction", "received_at", "redacted_body",
    }
    assert "<PERSON_2>" in msg["redacted_body"]


def test_thread_detail_blanks_unredacted_body(client: TestClient):
    """parent_c has body_redacted=0 — body must come back empty, never the raw
    SSN-containing text."""
    tid = _resolve_thread_id_from_list(client, "2026-05-05T08:00:00Z")
    r = client.get(f"/v1/threads/{tid}", headers=_auth())
    assert r.status_code == 200
    body = r.json()
    assert body["unredacted_message_count"] == 1
    raw = json.dumps(body)
    assert "123-45-6789" not in raw
    assert "SSN" not in raw
    assert body["messages"][0]["redacted_body"] == ""


def test_thread_detail_unknown_id_returns_404(client: TestClient):
    r = client.get(f"/v1/threads/{'0' * 16}", headers=_auth())
    assert r.status_code == 404


def test_thread_detail_orders_messages_by_received_at(client: TestClient):
    tid = _resolve_thread_id_from_list(client, "2026-04-11T09:00:00Z")  # parent_a
    r = client.get(f"/v1/threads/{tid}", headers=_auth())
    msgs = r.json()["messages"]
    assert len(msgs) == 2
    assert msgs[0]["direction"] == "inbound"
    assert msgs[1]["direction"] == "outbound"
    assert msgs[0]["message_index"] == 0
    assert msgs[1]["message_index"] == 1


def test_thread_detail_payload_omits_raw_pii_fields(client: TestClient):
    tid = _resolve_thread_id_from_list(client, "2026-04-11T09:00:00Z")
    body = client.get(f"/v1/threads/{tid}", headers=_auth()).text
    assert "parent_a@example.com" not in body
    assert "Parent A" not in body
    assert "Re: Medical forms question" not in body
    assert "m_a1" not in body
    assert "m_a2" not in body


# ---- search -------------------------------------------------------------

def test_search_validates_payload(client: TestClient):
    r = client.post("/v1/search", headers=_auth(), json={"question": ""})
    assert r.status_code == 422


def test_search_top_k_bounds(client: TestClient):
    r = client.post("/v1/search", headers=_auth(),
                    json={"question": "anything", "top_k": 999})
    assert r.status_code == 422


def test_search_calls_underlying_function(client: TestClient, monkeypatch):
    """Don't load sentence-transformers in the test path. Monkeypatch the
    impl to a stub and confirm the route wires args through correctly."""
    captured = {}

    def fake_search(config, store, question, top_k, category, include_stale):
        captured.update(
            question=question, top_k=top_k,
            category=category, include_stale=include_stale,
        )
        return [
            {"qa_id": "abc", "canonical_question": "stub Q", "canonical_answer": "stub A",
             "category": "health_safety", "received_year": 2025,
             "score": 0.9, "cosine": 0.95, "recency": 0.94,
             "evergreen_score": 0.0, "stale_reason": None},
        ]

    import sasi_mcp.mcp_server as mcp_mod
    monkeypatch.setattr(mcp_mod, "search_sasi_faq", fake_search)

    r = client.post(
        "/v1/search", headers=_auth(),
        json={"question": "When are forms due?", "top_k": 3,
              "exclude_stale": True, "category": "health_safety"},
    )
    assert r.status_code == 200
    body = r.json()
    assert len(body["matches"]) == 1
    assert body["matches"][0]["qa_id"] == "abc"
    # exclude_stale=True translates to include_stale=False.
    assert captured == {
        "question": "When are forms due?",
        "top_k": 3,
        "category": "health_safety",
        "include_stale": False,
    }
