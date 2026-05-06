"""SQLite schema + DAO for the SASI FAQ corpus.

Single file at config.db_path (default ~/.sasi-mcp/corpus.sqlite). Vectors
are stored as packed float32 BLOBs in `embeddings.vector`; sqlite-vss is
optional and only worthwhile at >100k vectors. For a few thousand pairs,
brute-force cosine in Python is fine.
"""

from __future__ import annotations

import json
import sqlite3
import struct
import time
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path

SCHEMA_VERSION = 1


@dataclass(slots=True)
class MessageRecord:
    message_id: str
    conversation_id: str
    account: str
    folder: str  # "inbox" | "sent"
    direction: str  # "inbound" | "outbound"
    received_at: str  # ISO 8601
    sender_email: str
    sender_name: str
    recipients: list[dict[str, str]] = field(default_factory=list)
    subject: str = ""
    body_text: str = ""
    body_redacted: int = 0  # 0 = raw, 1 = redacted
    ingested_at: str = ""


@dataclass(slots=True)
class QAPair:
    qa_id: str
    conversation_key: str
    question_message_id: str
    answer_message_id: str
    received_at: str
    category: str | None = None
    canonical_question: str | None = None
    canonical_answer: str | None = None
    review_status: str = "pending"  # pending | approved | rejected
    reviewer: str | None = None
    reviewed_at: str | None = None
    supersedes_qa_id: str | None = None
    stale_reason: str | None = None
    user_overridden_stale: int = 0
    temporal_anchors: list[str] = field(default_factory=list)
    year_bound: int | None = None
    evergreen_score: float = 0.0
    cluster_id: int | None = None
    classifier_confidence: float | None = None
    extract_confidence: float | None = None
    created_at: str = ""


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS messages (
    message_id          TEXT PRIMARY KEY,
    conversation_id     TEXT,
    account             TEXT NOT NULL,
    folder              TEXT NOT NULL,
    direction           TEXT NOT NULL,
    received_at         TEXT NOT NULL,
    sender_email        TEXT NOT NULL,
    sender_name         TEXT,
    recipients_json     TEXT,
    subject             TEXT,
    body_text           TEXT,
    body_redacted       INTEGER NOT NULL DEFAULT 0,
    ingested_at         TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_conversation ON messages(conversation_id);
CREATE INDEX IF NOT EXISTS idx_messages_received_at ON messages(received_at);
CREATE INDEX IF NOT EXISTS idx_messages_redacted ON messages(body_redacted);

CREATE TABLE IF NOT EXISTS threads (
    conversation_key    TEXT PRIMARY KEY,
    first_received_at   TEXT NOT NULL,
    last_received_at    TEXT NOT NULL,
    message_count       INTEGER NOT NULL,
    has_outbound        INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS qa_pairs (
    qa_id                   TEXT PRIMARY KEY,
    conversation_key        TEXT NOT NULL,
    question_message_id     TEXT NOT NULL,
    answer_message_id       TEXT NOT NULL,
    received_at             TEXT NOT NULL,
    category                TEXT,
    canonical_question      TEXT,
    canonical_answer        TEXT,
    review_status           TEXT NOT NULL DEFAULT 'pending',
    reviewer                TEXT,
    reviewed_at             TEXT,
    supersedes_qa_id        TEXT,
    stale_reason            TEXT,
    user_overridden_stale   INTEGER NOT NULL DEFAULT 0,
    temporal_anchors_json   TEXT,
    year_bound              INTEGER,
    evergreen_score         REAL NOT NULL DEFAULT 0,
    cluster_id              INTEGER,
    classifier_confidence   REAL,
    extract_confidence      REAL,
    created_at              TEXT NOT NULL,
    UNIQUE(question_message_id, answer_message_id)
);
CREATE INDEX IF NOT EXISTS idx_qa_review_status ON qa_pairs(review_status);
CREATE INDEX IF NOT EXISTS idx_qa_category ON qa_pairs(category);
CREATE INDEX IF NOT EXISTS idx_qa_year_bound ON qa_pairs(year_bound);
CREATE INDEX IF NOT EXISTS idx_qa_stale ON qa_pairs(stale_reason);

CREATE TABLE IF NOT EXISTS embeddings (
    qa_id   TEXT PRIMARY KEY,
    vector  BLOB NOT NULL,
    model   TEXT NOT NULL,
    dim     INTEGER NOT NULL,
    FOREIGN KEY (qa_id) REFERENCES qa_pairs(qa_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS query_log (
    query_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    ts              TEXT NOT NULL,
    question        TEXT NOT NULL,
    top_k_qa_ids    TEXT,
    top_score       REAL,
    follow_up_seen  INTEGER NOT NULL DEFAULT 0,
    flagged_reason  TEXT,
    reviewed_status TEXT
);

CREATE TABLE IF NOT EXISTS coverage_clusters (
    cluster_id      INTEGER PRIMARY KEY,
    label           TEXT,
    member_count    INTEGER NOT NULL DEFAULT 0,
    centroid        BLOB,
    last_audited_at TEXT
);

CREATE TABLE IF NOT EXISTS meta (
    key     TEXT PRIMARY KEY,
    value   TEXT
);
"""


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def pack_vector(vec: Iterable[float]) -> bytes:
    floats = list(vec)
    return struct.pack(f"{len(floats)}f", *floats)


def unpack_vector(blob: bytes) -> list[float]:
    count = len(blob) // 4
    return list(struct.unpack(f"{count}f", blob))


class Store:
    """Thin DAO over a single SQLite file. All methods are synchronous."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(db_path))
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.executescript(SCHEMA_SQL)
        self._conn.execute(
            "INSERT OR IGNORE INTO meta(key, value) VALUES('schema_version', ?)",
            (str(SCHEMA_VERSION),),
        )
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        try:
            yield self._conn
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise

    # ---- messages -------------------------------------------------------

    def upsert_message(self, m: MessageRecord) -> bool:
        """Insert or update a message. Returns True if newly inserted.

        Idempotent on `message_id`. If the row already exists AND has
        `body_redacted=1`, the body_text column is NOT overwritten — we
        never re-introduce raw text after redaction.
        """
        existing = self._conn.execute(
            "SELECT body_redacted FROM messages WHERE message_id = ?", (m.message_id,)
        ).fetchone()
        ingested_at = m.ingested_at or _now()
        if existing is None:
            self._conn.execute(
                """INSERT INTO messages(message_id, conversation_id, account, folder,
                       direction, received_at, sender_email, sender_name,
                       recipients_json, subject, body_text, body_redacted, ingested_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    m.message_id, m.conversation_id, m.account, m.folder,
                    m.direction, m.received_at, m.sender_email, m.sender_name,
                    json.dumps(m.recipients), m.subject, m.body_text, m.body_redacted,
                    ingested_at,
                ),
            )
            self._conn.commit()
            return True
        # Existing row — preserve already-redacted body if any.
        if int(existing["body_redacted"]) == 1:
            body_field_sql = ""
            params: tuple = (
                m.conversation_id, m.account, m.folder, m.direction, m.received_at,
                m.sender_email, m.sender_name, json.dumps(m.recipients), m.subject,
                m.message_id,
            )
        else:
            body_field_sql = ", body_text = ?, body_redacted = ?"
            params = (
                m.conversation_id, m.account, m.folder, m.direction, m.received_at,
                m.sender_email, m.sender_name, json.dumps(m.recipients), m.subject,
                m.body_text, m.body_redacted, m.message_id,
            )
        self._conn.execute(
            f"""UPDATE messages
                SET conversation_id=?, account=?, folder=?, direction=?,
                    received_at=?, sender_email=?, sender_name=?,
                    recipients_json=?, subject=?{body_field_sql}
                WHERE message_id = ?""",
            params,
        )
        self._conn.commit()
        return False

    def list_messages(self, *, account: str | None = None) -> list[MessageRecord]:
        sql = "SELECT * FROM messages"
        args: tuple = ()
        if account:
            sql += " WHERE account = ?"
            args = (account,)
        sql += " ORDER BY received_at ASC"
        rows = self._conn.execute(sql, args).fetchall()
        return [_row_to_message(r) for r in rows]

    def list_unredacted(self) -> list[MessageRecord]:
        rows = self._conn.execute(
            "SELECT * FROM messages WHERE body_redacted = 0"
        ).fetchall()
        return [_row_to_message(r) for r in rows]

    def update_redacted_body(self, message_id: str, redacted_text: str) -> None:
        self._conn.execute(
            "UPDATE messages SET body_text = ?, body_redacted = 1 WHERE message_id = ?",
            (redacted_text, message_id),
        )
        self._conn.commit()

    # ---- threads --------------------------------------------------------

    def replace_threads(self, threads: list[dict]) -> None:
        with self.transaction() as conn:
            conn.execute("DELETE FROM threads")
            conn.executemany(
                """INSERT INTO threads(conversation_key, first_received_at,
                       last_received_at, message_count, has_outbound)
                   VALUES(?,?,?,?,?)""",
                [
                    (t["conversation_key"], t["first_received_at"],
                     t["last_received_at"], t["message_count"], t["has_outbound"])
                    for t in threads
                ],
            )

    # ---- qa_pairs -------------------------------------------------------

    def upsert_qa_pair(self, qa: QAPair) -> bool:
        created_at = qa.created_at or _now()
        try:
            self._conn.execute(
                """INSERT INTO qa_pairs(qa_id, conversation_key, question_message_id,
                        answer_message_id, received_at, category, canonical_question,
                        canonical_answer, review_status, reviewer, reviewed_at,
                        supersedes_qa_id, stale_reason, user_overridden_stale,
                        temporal_anchors_json, year_bound, evergreen_score,
                        cluster_id, classifier_confidence, extract_confidence,
                        created_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    qa.qa_id, qa.conversation_key, qa.question_message_id,
                    qa.answer_message_id, qa.received_at, qa.category,
                    qa.canonical_question, qa.canonical_answer, qa.review_status,
                    qa.reviewer, qa.reviewed_at, qa.supersedes_qa_id, qa.stale_reason,
                    qa.user_overridden_stale, json.dumps(qa.temporal_anchors),
                    qa.year_bound, qa.evergreen_score, qa.cluster_id,
                    qa.classifier_confidence, qa.extract_confidence, created_at,
                ),
            )
            self._conn.commit()
            return True
        except sqlite3.IntegrityError:
            # Pair already exists for this (question, answer) — leave existing row.
            return False

    def update_qa_extract(
        self,
        qa_id: str,
        canonical_question: str,
        canonical_answer: str,
        temporal_anchors: list[str],
        year_bound: int | None,
        confidence: float,
    ) -> None:
        self._conn.execute(
            """UPDATE qa_pairs SET canonical_question = ?, canonical_answer = ?,
                  temporal_anchors_json = ?, year_bound = ?, extract_confidence = ?
               WHERE qa_id = ?""",
            (
                canonical_question, canonical_answer, json.dumps(temporal_anchors),
                year_bound, confidence, qa_id,
            ),
        )
        self._conn.commit()

    def update_qa_category(self, qa_id: str, category: str, confidence: float) -> None:
        self._conn.execute(
            "UPDATE qa_pairs SET category = ?, classifier_confidence = ? WHERE qa_id = ?",
            (category, confidence, qa_id),
        )
        self._conn.commit()

    def update_qa_review(
        self,
        qa_id: str,
        status: str,
        reviewer: str,
        canonical_question: str | None = None,
        canonical_answer: str | None = None,
    ) -> None:
        if canonical_question is not None and canonical_answer is not None:
            self._conn.execute(
                """UPDATE qa_pairs SET review_status=?, reviewer=?, reviewed_at=?,
                          canonical_question=?, canonical_answer=?
                   WHERE qa_id=?""",
                (status, reviewer, _now(), canonical_question, canonical_answer, qa_id),
            )
        else:
            self._conn.execute(
                """UPDATE qa_pairs SET review_status=?, reviewer=?, reviewed_at=?
                   WHERE qa_id=?""",
                (status, reviewer, _now(), qa_id),
            )
        self._conn.commit()

    def mark_stale(self, qa_id: str, reason: str) -> None:
        self._conn.execute(
            "UPDATE qa_pairs SET stale_reason = ? WHERE qa_id = ?",
            (reason, qa_id),
        )
        self._conn.commit()

    def reaffirm(self, qa_id: str) -> None:
        self._conn.execute(
            "UPDATE qa_pairs SET stale_reason = NULL, user_overridden_stale = 1 WHERE qa_id = ?",
            (qa_id,),
        )
        self._conn.commit()

    def supersede(self, old_qa_id: str, new_qa_id: str) -> None:
        with self.transaction() as conn:
            conn.execute(
                "UPDATE qa_pairs SET supersedes_qa_id = ? WHERE qa_id = ?",
                (old_qa_id, new_qa_id),
            )
            conn.execute(
                "UPDATE qa_pairs SET stale_reason = 'superseded' WHERE qa_id = ?",
                (old_qa_id,),
            )

    def set_evergreen(self, qa_id: str, score: float, cluster_id: int | None = None) -> None:
        if cluster_id is None:
            self._conn.execute(
                "UPDATE qa_pairs SET evergreen_score = ? WHERE qa_id = ?",
                (score, qa_id),
            )
        else:
            self._conn.execute(
                "UPDATE qa_pairs SET evergreen_score = ?, cluster_id = ? WHERE qa_id = ?",
                (score, cluster_id, qa_id),
            )
        self._conn.commit()

    def get_qa_pair(self, qa_id: str) -> QAPair | None:
        row = self._conn.execute(
            "SELECT * FROM qa_pairs WHERE qa_id = ?", (qa_id,)
        ).fetchone()
        return _row_to_qa(row) if row else None

    def list_qa_pairs(
        self,
        *,
        status: str | None = None,
        category: str | None = None,
        include_stale: bool = True,
    ) -> list[QAPair]:
        clauses: list[str] = []
        args: list = []
        if status:
            clauses.append("review_status = ?")
            args.append(status)
        if category:
            clauses.append("category = ?")
            args.append(category)
        if not include_stale:
            clauses.append("(stale_reason IS NULL OR user_overridden_stale = 1)")
        sql = "SELECT * FROM qa_pairs"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY received_at ASC"
        rows = self._conn.execute(sql, tuple(args)).fetchall()
        return [_row_to_qa(r) for r in rows]

    # ---- embeddings -----------------------------------------------------

    def upsert_embedding(self, qa_id: str, vector: list[float], model: str) -> None:
        blob = pack_vector(vector)
        self._conn.execute(
            """INSERT INTO embeddings(qa_id, vector, model, dim) VALUES(?,?,?,?)
               ON CONFLICT(qa_id) DO UPDATE SET vector=excluded.vector,
                     model=excluded.model, dim=excluded.dim""",
            (qa_id, blob, model, len(vector)),
        )
        self._conn.commit()

    def list_embeddings(self) -> list[tuple[str, list[float], str]]:
        rows = self._conn.execute(
            "SELECT qa_id, vector, model FROM embeddings"
        ).fetchall()
        return [(r["qa_id"], unpack_vector(r["vector"]), r["model"]) for r in rows]

    def delete_embedding(self, qa_id: str) -> None:
        self._conn.execute("DELETE FROM embeddings WHERE qa_id = ?", (qa_id,))
        self._conn.commit()

    # ---- query_log ------------------------------------------------------

    def log_query(
        self,
        question: str,
        top_k_qa_ids: list[str],
        top_score: float | None,
    ) -> int:
        cur = self._conn.execute(
            """INSERT INTO query_log(ts, question, top_k_qa_ids, top_score)
               VALUES(?,?,?,?)""",
            (_now(), question, json.dumps(top_k_qa_ids), top_score),
        )
        self._conn.commit()
        return int(cur.lastrowid or 0)

    def flag_query(self, query_id: int, reason: str) -> None:
        self._conn.execute(
            "UPDATE query_log SET flagged_reason = ? WHERE query_id = ?",
            (reason, query_id),
        )
        self._conn.commit()

    def list_recent_queries(self, limit: int = 20) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM query_log ORDER BY query_id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]

    # ---- meta -----------------------------------------------------------

    def set_meta(self, key: str, value: str) -> None:
        self._conn.execute(
            "INSERT INTO meta(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )
        self._conn.commit()

    def get_meta(self, key: str) -> str | None:
        row = self._conn.execute(
            "SELECT value FROM meta WHERE key = ?", (key,)
        ).fetchone()
        return row[0] if row else None

    def stats(self) -> dict:
        c = self._conn.execute
        return {
            "messages": c("SELECT COUNT(*) FROM messages").fetchone()[0],
            "messages_redacted": c(
                "SELECT COUNT(*) FROM messages WHERE body_redacted = 1"
            ).fetchone()[0],
            "qa_pairs": c("SELECT COUNT(*) FROM qa_pairs").fetchone()[0],
            "qa_approved": c(
                "SELECT COUNT(*) FROM qa_pairs WHERE review_status = 'approved'"
            ).fetchone()[0],
            "qa_pending": c(
                "SELECT COUNT(*) FROM qa_pairs WHERE review_status = 'pending'"
            ).fetchone()[0],
            "embeddings": c("SELECT COUNT(*) FROM embeddings").fetchone()[0],
            "last_ingest_at": self.get_meta("last_ingest_at"),
        }


def _row_to_message(r: sqlite3.Row) -> MessageRecord:
    return MessageRecord(
        message_id=r["message_id"],
        conversation_id=r["conversation_id"] or "",
        account=r["account"],
        folder=r["folder"],
        direction=r["direction"],
        received_at=r["received_at"],
        sender_email=r["sender_email"],
        sender_name=r["sender_name"] or "",
        recipients=json.loads(r["recipients_json"] or "[]"),
        subject=r["subject"] or "",
        body_text=r["body_text"] or "",
        body_redacted=int(r["body_redacted"]),
        ingested_at=r["ingested_at"],
    )


def _row_to_qa(r: sqlite3.Row) -> QAPair:
    return QAPair(
        qa_id=r["qa_id"],
        conversation_key=r["conversation_key"],
        question_message_id=r["question_message_id"],
        answer_message_id=r["answer_message_id"],
        received_at=r["received_at"],
        category=r["category"],
        canonical_question=r["canonical_question"],
        canonical_answer=r["canonical_answer"],
        review_status=r["review_status"],
        reviewer=r["reviewer"],
        reviewed_at=r["reviewed_at"],
        supersedes_qa_id=r["supersedes_qa_id"],
        stale_reason=r["stale_reason"],
        user_overridden_stale=int(r["user_overridden_stale"]),
        temporal_anchors=json.loads(r["temporal_anchors_json"] or "[]"),
        year_bound=r["year_bound"],
        evergreen_score=float(r["evergreen_score"] or 0),
        cluster_id=r["cluster_id"],
        classifier_confidence=r["classifier_confidence"],
        extract_confidence=r["extract_confidence"],
        created_at=r["created_at"],
    )


# Hook for `dataclasses.asdict` round-trips in tests.
def qa_to_dict(qa: QAPair) -> dict:
    return asdict(qa)
