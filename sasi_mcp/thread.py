"""Group messages into conversations and pair inbound questions with outbound replies.

Pairing rule (per the plan):
- Group messages by `conversation_key`.
- Within a thread, sort by `received_at`.
- For each inbound message, the **next** outbound message in the same thread
  forms one Q&A pair. A back-and-forth thread can yield multiple pairs.
- Skip threads with no outbound. Skip outbound-only threads.

Conversation-key fallback (important): if Outlook's `conversation_id` is
missing/empty, hash on `_norm_subject(subject)` ALONE — NOT subject +
sender_domain. In a question/answer exchange the inbound is from the parent's
domain and the outbound is from stanford.edu; mixing domain into the key
would prevent the pair from ever colliding.
"""

from __future__ import annotations

import hashlib
import re
import time
from collections import defaultdict
from dataclasses import dataclass

from sasi_mcp.store import MessageRecord, QAPair

_RE_PREFIX = re.compile(r"^(re|fwd|fw|aw)\s*:\s*", re.IGNORECASE)


def _norm_subject(subject: str) -> str:
    """Lowercase + strip Re:/Fwd:/Fw: chains + collapse whitespace."""
    s = (subject or "").strip()
    while True:
        m = _RE_PREFIX.match(s)
        if not m:
            break
        s = s[m.end():].strip()
    s = re.sub(r"\s+", " ", s).lower()
    return s


def conversation_key(message: MessageRecord) -> str:
    """Stable thread key. Prefer Outlook's conversation_id; else hash subject only."""
    if message.conversation_id:
        return f"cid:{message.conversation_id}"
    norm = _norm_subject(message.subject)
    if not norm:
        # Truly empty subject — fall back to a per-message bucket so isolated
        # messages never accidentally co-group with each other.
        return f"msg:{message.message_id}"
    digest = hashlib.sha1(norm.encode("utf-8")).hexdigest()[:16]
    return f"sub:{digest}"


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _qa_id(question_id: str, answer_id: str) -> str:
    raw = f"{question_id}::{answer_id}".encode("utf-8")
    return hashlib.sha1(raw).hexdigest()[:20]


@dataclass(slots=True)
class ThreadSummary:
    conversation_key: str
    first_received_at: str
    last_received_at: str
    message_count: int
    has_outbound: bool


def group_threads(
    messages: list[MessageRecord],
) -> dict[str, list[MessageRecord]]:
    """Group messages by conversation_key. Within each thread, sort ascending by received_at."""
    by_key: dict[str, list[MessageRecord]] = defaultdict(list)
    for m in messages:
        by_key[conversation_key(m)].append(m)
    for key in by_key:
        by_key[key].sort(key=lambda m: (m.received_at, m.message_id))
    return dict(by_key)


def summarize_threads(grouped: dict[str, list[MessageRecord]]) -> list[ThreadSummary]:
    out: list[ThreadSummary] = []
    for key, msgs in grouped.items():
        out.append(
            ThreadSummary(
                conversation_key=key,
                first_received_at=msgs[0].received_at,
                last_received_at=msgs[-1].received_at,
                message_count=len(msgs),
                has_outbound=any(m.direction == "outbound" for m in msgs),
            )
        )
    return out


def derive_qa_pairs(messages: list[MessageRecord]) -> list[QAPair]:
    """Pair each inbound with the *next* outbound in the same thread.

    Returns a list of QAPair objects with `review_status='pending'`. Idempotent
    on (question_message_id, answer_message_id) — caller should upsert.
    """
    pairs: list[QAPair] = []
    grouped = group_threads(messages)
    now = _now()
    for key, msgs in grouped.items():
        if not any(m.direction == "outbound" for m in msgs):
            continue  # we never replied
        if not any(m.direction == "inbound" for m in msgs):
            continue  # outbound-only (we initiated)
        i = 0
        while i < len(msgs):
            if msgs[i].direction != "inbound":
                i += 1
                continue
            # Find the next outbound after this inbound.
            j = i + 1
            while j < len(msgs) and msgs[j].direction != "outbound":
                j += 1
            if j >= len(msgs):
                break
            q = msgs[i]
            a = msgs[j]
            pairs.append(
                QAPair(
                    qa_id=_qa_id(q.message_id, a.message_id),
                    conversation_key=key,
                    question_message_id=q.message_id,
                    answer_message_id=a.message_id,
                    received_at=a.received_at,
                    review_status="pending",
                    created_at=now,
                )
            )
            # Advance past this answer so a follow-up inbound starts a new pair.
            i = j + 1
    return pairs
