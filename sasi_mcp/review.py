"""Human-in-the-loop review TUI.

Minimal: works in any TTY without curses. Shows one pending pair at a time
with single-key actions:
    [a] approve   [r] reject   [e] edit (opens $EDITOR)   [s] skip   [q] quit

`review --auto-approve --confidence 0.85` bulk-approves pairs whose extract
and classifier confidences both clear the threshold. Off by default.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from collections.abc import Iterable
from dataclasses import dataclass

from sasi_mcp.logger import get_logger
from sasi_mcp.quality import is_quality_pair
from sasi_mcp.store import QAPair, Store

_log = get_logger("sasi_mcp.review")


@dataclass(slots=True)
class ReviewCounts:
    approved: int = 0
    rejected: int = 0
    skipped: int = 0
    edited: int = 0


def _edit_in_editor(initial: str) -> str:
    editor = os.environ.get("EDITOR") or "vi"
    with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False) as f:
        f.write(initial)
        path = f.name
    try:
        subprocess.call([editor, path])
        with open(path, encoding="utf-8") as f:
            return f.read()
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


def _render(pair: QAPair, idx: int, total: int) -> str:
    return (
        f"\n[QA {idx} / {total}]   qa_id={pair.qa_id}   cat={pair.category or '?'}   "
        f"received={pair.received_at[:7] if pair.received_at else '?'}\n"
        f"Q: {pair.canonical_question or '(no canonical question)'}\n"
        f"A: {pair.canonical_answer or '(no canonical answer)'}\n"
        "[a]pprove  [r]eject  [e]dit  [s]kip  [q]uit"
    )


def review_interactive(store: Store, reviewer: str, statuses: Iterable[str] = ("pending",)) -> ReviewCounts:
    pending: list[QAPair] = []
    for status in statuses:
        pending.extend(store.list_qa_pairs(status=status))
    pending.sort(key=lambda p: p.received_at)
    counts = ReviewCounts()
    if not pending:
        print("Nothing pending.")
        return counts
    total = len(pending)
    for idx, pair in enumerate(pending, 1):
        print(_render(pair, idx, total))
        try:
            choice = (input("> ").strip().lower() or "s")[:1]
        except (EOFError, KeyboardInterrupt):
            print("\nquitting")
            break
        if choice == "a":
            store.update_qa_review(pair.qa_id, "approved", reviewer)
            counts.approved += 1
        elif choice == "r":
            store.update_qa_review(pair.qa_id, "rejected", reviewer)
            counts.rejected += 1
        elif choice == "e":
            initial = (
                f"# canonical_question\n{pair.canonical_question or ''}\n\n"
                f"# canonical_answer\n{pair.canonical_answer or ''}\n"
            )
            edited = _edit_in_editor(initial)
            cq, ca = _split_edited(edited, pair)
            store.update_qa_review(pair.qa_id, "approved", reviewer,
                                   canonical_question=cq, canonical_answer=ca)
            counts.edited += 1
            counts.approved += 1
        elif choice == "q":
            break
        else:
            counts.skipped += 1
    print(f"approved={counts.approved} rejected={counts.rejected} "
          f"edited={counts.edited} skipped={counts.skipped}")
    return counts


def _split_edited(text: str, pair: QAPair) -> tuple[str, str]:
    """Parse the simple `# canonical_question` / `# canonical_answer` markdown
    template the editor opens with. Falls back to the original on parse failure."""
    parts = text.split("# canonical_answer", 1)
    if len(parts) != 2:
        return pair.canonical_question or "", pair.canonical_answer or ""
    head, tail = parts
    head = head.split("# canonical_question", 1)[-1].strip()
    return head, tail.strip()


def auto_approve(store: Store, threshold: float, reviewer: str = "auto") -> int:
    pending = store.list_qa_pairs(status="pending")
    n = 0
    rejected = 0
    for p in pending:
        if not (
            p.classifier_confidence is not None
            and p.extract_confidence is not None
            and p.classifier_confidence >= threshold
            and p.extract_confidence >= threshold
            and p.canonical_question
            and p.canonical_answer
        ):
            continue
        ok, _ = is_quality_pair(p.canonical_question, p.canonical_answer)
        if not ok:
            rejected += 1
            continue
        store.update_qa_review(p.qa_id, "approved", reviewer)
        n += 1
    print(
        f"auto-approved {n} pairs at threshold {threshold:.2f} "
        f"(quality-rejected {rejected})",
        file=sys.stderr,
    )
    return n


def quality_audit(store: Store, *, apply: bool = False) -> dict[str, int]:
    """Walk approved pairs, demote any that fail the quality filter back to pending
    and drop their embeddings so retrieval stops surfacing them.

    `apply=False` is a dry run: returns the histogram of failure reasons without
    touching the DB. `apply=True` executes the demotions."""
    approved = store.list_qa_pairs(status="approved")
    by_reason: dict[str, int] = {}
    demoted_ids: list[str] = []
    for p in approved:
        ok, reason = is_quality_pair(p.canonical_question, p.canonical_answer)
        if ok:
            continue
        by_reason[reason] = by_reason.get(reason, 0) + 1
        demoted_ids.append(p.qa_id)
    if apply:
        for qid in demoted_ids:
            store.update_qa_review(qid, "pending", "quality_audit")
            store.delete_embedding(qid)
    return {"approved_seen": len(approved), "demoted": len(demoted_ids), **{f"reason_{k}": v for k, v in by_reason.items()}}
