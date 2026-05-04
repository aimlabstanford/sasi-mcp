"""Read messages from a shared Outlook mailbox via AppleScript.

Thin wrapper around `applescript/list_thread_messages.applescript`. Handles
the chunked driver loop for backfill (windowed days_back ranges) so the
AppleScript timeout doesn't stretch.
"""

from __future__ import annotations

import time
from collections.abc import Iterable
from datetime import datetime, timedelta, timezone

from sasi_mcp._outlook_bridge import OutlookError, _run_script
from sasi_mcp.logger import get_logger
from sasi_mcp.store import MessageRecord

_log = get_logger("sasi_mcp.outlook_reader")

_CHUNK_DAYS = 90  # window size for the chunked backfill driver


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _direction(sender_email: str, mailbox_email: str) -> str:
    return "outbound" if sender_email.lower() == mailbox_email.lower() else "inbound"


def read_folder(
    mailbox_email: str,
    folder_kind: str,
    days_back: int,
    limit: int = 1000,
) -> list[MessageRecord]:
    """Run the AppleScript once and return parsed MessageRecords."""
    if folder_kind not in {"inbox", "sent"}:
        raise ValueError(f"folder_kind must be 'inbox' or 'sent', got {folder_kind!r}")
    raw = _run_script(
        "list_thread_messages.applescript",
        mailbox_email,
        folder_kind,
        str(days_back),
        str(limit),
    )
    if raw is None:
        return []
    if isinstance(raw, dict) and raw.get("error"):
        raise OutlookError(f"applescript error: {raw}")
    if not isinstance(raw, list):
        raise OutlookError(f"applescript returned non-list: {type(raw).__name__}")
    out: list[MessageRecord] = []
    ingested_at = _now_iso()
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        sender_email = str(entry.get("sender_email", "")).lower()
        recipients = []
        for r in (entry.get("recipients_to") or []) + (entry.get("recipients_cc") or []):
            if isinstance(r, dict):
                recipients.append(
                    {"name": str(r.get("name", "")), "email": str(r.get("email", "")).lower()}
                )
        out.append(
            MessageRecord(
                message_id=str(entry.get("message_id", "")),
                conversation_id=str(entry.get("conversation_id", "") or ""),
                account=str(entry.get("account", mailbox_email)),
                folder=str(entry.get("folder", folder_kind)),
                direction=_direction(sender_email, mailbox_email),
                received_at=str(entry.get("received_at", "")),
                sender_email=sender_email,
                sender_name=str(entry.get("sender_name", "")),
                recipients=recipients,
                subject=str(entry.get("subject", "")),
                body_text=str(entry.get("body_text", "")),
                body_redacted=0,
                ingested_at=ingested_at,
            )
        )
    return out


def read_folder_chunked(
    mailbox_email: str,
    folder_kind: str,
    since: datetime,
    until: datetime | None = None,
    chunk_days: int = _CHUNK_DAYS,
    per_chunk_limit: int = 1000,
) -> Iterable[MessageRecord]:
    """Yield messages in `chunk_days`-wide windows from `since` to `until`.

    The AppleScript can only express days_back as a single number. To backfill
    multi-year history without bumping its timeout, we walk forward in chunks
    by translating each chunk's right edge into a fresh days_back from "now".
    Duplicates across chunks are filtered out by message_id.
    """
    until = until or datetime.now(timezone.utc)
    seen: set[str] = set()
    cursor = since
    while cursor < until:
        end = min(cursor + timedelta(days=chunk_days), until)
        days_back = max(1, (datetime.now(timezone.utc) - cursor).days + 1)
        # AppleScript returns everything within the last `days_back`. We then
        # clip to messages that fall in [cursor, end).
        try:
            messages = read_folder(
                mailbox_email, folder_kind, days_back=days_back, limit=per_chunk_limit
            )
        except OutlookError as exc:
            _log.warning("outlook_reader.chunk_failed", error=str(exc), since=str(cursor))
            messages = []
        for m in messages:
            if m.message_id in seen:
                continue
            seen.add(m.message_id)
            try:
                m_dt = datetime.fromisoformat(m.received_at.replace("Z", "+00:00"))
            except ValueError:
                continue
            if cursor <= m_dt < end:
                yield m
        cursor = end
