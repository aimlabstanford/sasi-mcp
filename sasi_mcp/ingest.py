"""Ingest driver: read both folders, upsert messages, recompute threads, derive Q&A pairs."""

from __future__ import annotations

import time
from datetime import datetime, timezone

from sasi_mcp.config import OutlookConfig
from sasi_mcp.logger import get_logger
from sasi_mcp.outlook_reader import list_account_folders, read_folder
from sasi_mcp.store import MessageRecord, Store
from sasi_mcp.thread import derive_qa_pairs, group_threads, summarize_threads

_log = get_logger("sasi_mcp.ingest")

# Folders we never want to ingest even if they belong to the mailbox.
# "Conversation History" is Teams/Zoom IM logs (different shape, no Q&A);
# "Drafts" never sent; "Deleted Items" / "Junk" are noise. Outbox/Sync
# Issues are Outlook plumbing.
_SKIP_FOLDERS = {
    "Drafts", "Deleted Items", "Junk Email", "Junk E-mail", "Junk",
    "Outbox", "Conversation History", "Sync Issues", "Local Failures",
    "Server Failures", "Conflicts", "Spam",
}


def ingest(
    store: Store,
    outlook: OutlookConfig,
    *,
    since: datetime | None = None,
    limit: int = 1000,
) -> dict[str, int]:
    """Read summermed@ Inbox + Sent, upsert messages, derive Q&A pairs.

    Returns counts: {messages_seen, messages_new, qa_new, threads}.
    """
    if since is not None:
        delta = datetime.now(timezone.utc) - since
        days = max(1, delta.days + 1)
    else:
        days = max(1, outlook.days_back)

    seen_messages: list[MessageRecord] = []
    for kind in outlook.folders:
        _log.info("ingest.folder_start", kind=kind, days_back=days, limit=limit)
        seen_messages.extend(
            read_folder(outlook.mailbox_email, kind, days_back=days, limit=limit)
        )

    new_count = 0
    for m in seen_messages:
        if store.upsert_message(m):
            new_count += 1

    all_messages = store.list_messages(account=outlook.mailbox_email)
    grouped = group_threads(all_messages)
    summaries = summarize_threads(grouped)
    store.replace_threads([
        {
            "conversation_key": s.conversation_key,
            "first_received_at": s.first_received_at,
            "last_received_at": s.last_received_at,
            "message_count": s.message_count,
            "has_outbound": int(s.has_outbound),
        }
        for s in summaries
    ])

    pairs = derive_qa_pairs(all_messages)
    qa_new = 0
    for pair in pairs:
        if store.upsert_qa_pair(pair):
            qa_new += 1

    store.set_meta("last_ingest_at", time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
    return {
        "messages_seen": len(seen_messages),
        "messages_new": new_count,
        "qa_new": qa_new,
        "threads": len(summaries),
    }


def ingest_all_folders(
    store: Store,
    outlook: OutlookConfig,
    *,
    days_back: int,
    limit_per_folder: int = 5000,
    skip: set[str] | None = None,
) -> dict[str, int]:
    """Discover every top-level folder owned by the mailbox, ingest each, then
    re-derive threads + Q&A pairs across the union. Skips noisy folders
    (Drafts / Deleted Items / Junk / Outlook plumbing)."""
    skip = skip if skip is not None else _SKIP_FOLDERS
    folders = list_account_folders(outlook.mailbox_email)
    targets = [f for f in folders if f["name"] not in skip]
    _log.info(
        "ingest_all_folders.discover",
        total=len(folders),
        targets=len(targets),
        skipped=[f["name"] for f in folders if f["name"] in skip],
    )

    seen_messages: list[MessageRecord] = []
    per_folder: list[dict] = []
    for f in sorted(targets, key=lambda x: -int(x["message_count"])):
        name = str(f["name"])
        count = int(f["message_count"])
        _log.info("ingest_all_folders.folder_start", name=name, expected=count,
                  days_back=days_back, limit=limit_per_folder)
        try:
            msgs = read_folder(outlook.mailbox_email, name, days_back=days_back,
                               limit=limit_per_folder)
        except Exception as exc:
            _log.warning("ingest_all_folders.folder_failed", name=name, error=str(exc))
            per_folder.append({"name": name, "expected": count, "fetched": 0,
                               "error": str(exc)})
            continue
        per_folder.append({"name": name, "expected": count, "fetched": len(msgs)})
        seen_messages.extend(msgs)

    new_count = 0
    for m in seen_messages:
        if store.upsert_message(m):
            new_count += 1

    all_messages = store.list_messages(account=outlook.mailbox_email)
    grouped = group_threads(all_messages)
    summaries = summarize_threads(grouped)
    store.replace_threads([
        {
            "conversation_key": s.conversation_key,
            "first_received_at": s.first_received_at,
            "last_received_at": s.last_received_at,
            "message_count": s.message_count,
            "has_outbound": int(s.has_outbound),
        }
        for s in summaries
    ])

    pairs = derive_qa_pairs(all_messages)
    qa_new = 0
    for pair in pairs:
        if store.upsert_qa_pair(pair):
            qa_new += 1

    store.set_meta("last_ingest_at", time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
    return {
        "folders_targeted": len(targets),
        "messages_seen": len(seen_messages),
        "messages_new": new_count,
        "qa_new": qa_new,
        "threads": len(summaries),
        "per_folder": per_folder,
    }


def parse_since(value: str | None) -> datetime | None:
    if not value:
        return None
    if value.endswith("Z"):
        value = value.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        # bare YYYY-MM-DD
        dt = datetime.strptime(value, "%Y-%m-%d")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt
