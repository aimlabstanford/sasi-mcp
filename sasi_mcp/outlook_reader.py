"""Read messages from a shared Outlook mailbox via AppleScript.

Thin wrapper around `applescript/list_thread_messages.applescript`. The
AppleScript walks newest-first by index and stops once `limit` is reached
or the date cutoff is exceeded; the inner walk caps at 5000 messages per
call. For larger backfills, raise `outlook.days_back` and `--limit`
together — the script terminates early on its own once the cutoff hits.
"""

from __future__ import annotations

import time

from sasi_mcp._outlook_bridge import OutlookError, _run_script
from sasi_mcp.logger import get_logger
from sasi_mcp.store import MessageRecord

_log = get_logger("sasi_mcp.outlook_reader")


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _direction(sender_email: str, mailbox_email: str) -> str:
    return "outbound" if sender_email.lower() == mailbox_email.lower() else "inbound"


def list_account_folders(mailbox_email: str) -> list[dict[str, object]]:
    """Return [{name, message_count}] for every top-level Outlook mail folder
    that belongs to `mailbox_email`. Folders with zero messages are omitted —
    Outlook 16's AppleScript model only lets us identify folder ownership by
    inspecting the first message's account, so empty folders are unprobeable
    (and uninteresting for ingest)."""
    raw = _run_script("list_account_folders.applescript", mailbox_email)
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise OutlookError(f"list_account_folders returned non-list: {type(raw).__name__}")
    out: list[dict[str, object]] = []
    for entry in raw:
        if isinstance(entry, dict) and entry.get("name"):
            out.append({
                "name": str(entry["name"]),
                "message_count": int(entry.get("message_count", 0)),
            })
    return out


def read_folder(
    mailbox_email: str,
    folder_kind: str,
    days_back: int,
    limit: int = 1000,
) -> list[MessageRecord]:
    """Run the AppleScript once and return parsed MessageRecords.

    `folder_kind` accepts the canonical aliases "inbox" / "sent" or any
    literal folder name owned by `mailbox_email` (used for subfolder ingest
    like "2018 Student and Parent Questions" or "Medical Forms Notification").
    """
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


