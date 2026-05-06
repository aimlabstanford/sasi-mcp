"""Read-only HTTP transport for the SASI corpus.

Sibling to the FastMCP stdio server (`mcp_server.serve`). Exposes the
same retrieval surface over HTTP so a Ledger-style Desk UI can fetch
threads + run searches without speaking MCP.

Privacy boundary (locked by Phase B review, see docs/phase-b-plan.md):
    Only redacted bodies and canonical Q&A cross the wire.
    No raw subjects, no sender_email / sender_name, no raw message_id,
    no raw conversation_key. Threads are addressed by an opaque HMAC
    thread_id. Bodies are read only when `body_redacted=1` — raw rows
    return empty body and the response carries an
    `unredacted_message_count` so the frontend can surface a notice.

Transport-agnostic by design: this module only knows about a bind
address and a bearer token. Cloudflare Tunnel / Tailscale / public TLS
are deployment concerns and live outside the codebase.
"""

from __future__ import annotations

import hashlib
import hmac
from collections.abc import Iterable
from typing import Any

from sasi_mcp.config import Config
from sasi_mcp.logger import get_logger
from sasi_mcp.store import MessageRecord, Store
from sasi_mcp.thread import conversation_key, group_threads

_log = get_logger("sasi_mcp.http_server")

_PREVIEW_CHARS = 200
_THREAD_ID_LEN = 16


# Pydantic models must live at module scope. When defined inside `create_app`
# Pydantic v2's TypeAdapter can't resolve the forward reference at request-
# validation time and crashes with "class is not fully defined."
try:
    from pydantic import BaseModel as _BaseModel
    from pydantic import Field as _Field

    class SearchRequest(_BaseModel):
        question: str = _Field(min_length=1, max_length=2000)
        top_k: int = _Field(default=5, ge=1, le=20)
        exclude_stale: bool = True
        category: str | None = None

except ImportError:
    # pydantic isn't installed in the bare minimal install; create_app raises
    # a friendlier error when the user tries to actually start the server.
    SearchRequest = None  # type: ignore[assignment,misc]


# ---- thread_id helpers --------------------------------------------------

def make_thread_id(secret: bytes, conv_key: str) -> str:
    """Deterministic HMAC of `conv_key`. Truncated to 16 hex chars (64 bits) —
    plenty against accidental collision on a corpus with <100k threads."""
    h = hmac.new(secret, conv_key.encode("utf-8"), hashlib.sha256)
    return h.hexdigest()[:_THREAD_ID_LEN]


def resolve_thread_id(secret: bytes, thread_id: str, conv_keys: Iterable[str]) -> str | None:
    """Reverse the HMAC by re-hashing every known conversation_key.

    Stateless: we don't persist a thread_id → conv_key map. For ~5k threads
    this is sub-millisecond — re-hash 5k strings, compare-equal."""
    target = thread_id.lower()
    for k in conv_keys:
        if hmac.compare_digest(make_thread_id(secret, k), target):
            return k
    return None


# ---- thread + message projections ---------------------------------------

def _redacted_preview(msg: MessageRecord) -> str:
    if msg.body_redacted != 1:
        return ""
    body = (msg.body_text or "").strip()
    if len(body) <= _PREVIEW_CHARS:
        return body
    return body[:_PREVIEW_CHARS].rstrip() + "…"


def _summarize_thread(thread_id: str, msgs: list[MessageRecord]) -> dict[str, Any]:
    msgs_sorted = sorted(msgs, key=lambda m: (m.received_at, m.message_id))
    latest = msgs_sorted[-1]
    return {
        "thread_id": thread_id,
        "first_received_at": msgs_sorted[0].received_at,
        "last_received_at": latest.received_at,
        "message_count": len(msgs_sorted),
        "has_outbound": any(m.direction == "outbound" for m in msgs_sorted),
        "latest_direction": latest.direction,
        "redacted_preview": _redacted_preview(latest),
    }


def _project_messages(msgs: list[MessageRecord]) -> tuple[list[dict[str, Any]], int]:
    msgs_sorted = sorted(msgs, key=lambda m: (m.received_at, m.message_id))
    out: list[dict[str, Any]] = []
    unredacted = 0
    for idx, m in enumerate(msgs_sorted):
        if m.body_redacted == 1:
            body = m.body_text or ""
        else:
            body = ""
            unredacted += 1
        out.append({
            "message_index": idx,
            "direction": m.direction,
            "received_at": m.received_at,
            "redacted_body": body,
        })
    return out, unredacted


def _is_needs_reply(msgs: list[MessageRecord]) -> bool:
    """Latest message inbound → thread needs a reply (per Phase B heuristic)."""
    if not msgs:
        return False
    latest = max(msgs, key=lambda m: (m.received_at, m.message_id))
    return latest.direction == "inbound"


# ---- FastAPI app builder ------------------------------------------------

def create_app(config: Config, *, token: str, secret: bytes) -> Any:
    """Build the FastAPI application. Importable for tests via TestClient.

    `token` is the bearer credential clients must present.
    `secret` is the HMAC key used for thread_id derivation.
    """
    try:
        from fastapi import Body, Depends, FastAPI, Header, HTTPException, Path
    except ImportError as exc:
        raise RuntimeError(
            "fastapi + pydantic are required. Install the [full] extra: "
            "pip install -e '.[full]'"
        ) from exc

    app = FastAPI(title="sasi-mcp http", version="0.1.0", docs_url=None, redoc_url=None)
    store = Store(config.db_path, check_same_thread=False)

    async def _require_bearer(authorization: str = Header(default="")) -> None:
        # Header() makes FastAPI auto-extract `Authorization` from the request,
        # avoiding the auto-inject ambiguity of typing a parameter as Request.
        if not authorization.lower().startswith("bearer "):
            raise HTTPException(status_code=401, detail="missing bearer token")
        provided = authorization.split(" ", 1)[1].strip()
        if not hmac.compare_digest(provided, token):
            raise HTTPException(status_code=401, detail="invalid bearer token")

    @app.get("/v1/healthz")
    def healthz() -> dict[str, Any]:
        # No auth — used for liveness checks.
        return {
            "status": "ok",
            "schema_version": int(store.get_meta("schema_version") or "1"),
            "embeddings": store.stats().get("embeddings", 0),
        }

    @app.get("/v1/threads", dependencies=[Depends(_require_bearer)])
    def list_threads(
        status: str = "needs_reply",
        limit: int = 50,
        since: str | None = None,
    ) -> dict[str, Any]:
        if status not in {"needs_reply", "all"}:
            raise HTTPException(status_code=400, detail="status must be needs_reply|all")
        if limit < 1 or limit > 200:
            raise HTTPException(status_code=400, detail="limit must be 1..200")

        messages = store.list_messages(account=config.outlook.mailbox_email)
        grouped = group_threads(messages)
        rows: list[tuple[str, str, list[MessageRecord]]] = []
        for conv_key, msgs in grouped.items():
            if status == "needs_reply" and not _is_needs_reply(msgs):
                continue
            last = max(m.received_at for m in msgs) if msgs else ""
            if since and last < since:
                continue
            tid = make_thread_id(secret, conv_key)
            rows.append((tid, last, msgs))

        rows.sort(key=lambda r: r[1], reverse=True)
        rows = rows[:limit]
        threads_out = [_summarize_thread(tid, msgs) for tid, _last, msgs in rows]
        pending_redaction_count = sum(
            1 for t in threads_out if t["redacted_preview"] == "" and t["latest_direction"] == "inbound"
        )
        return {
            "threads": threads_out,
            "total_returned": len(threads_out),
            "pending_redaction_count": pending_redaction_count,
        }

    @app.get("/v1/threads/{thread_id}", dependencies=[Depends(_require_bearer)])
    def get_thread(
        thread_id: str = Path(min_length=_THREAD_ID_LEN, max_length=_THREAD_ID_LEN),
    ) -> dict[str, Any]:
        messages = store.list_messages(account=config.outlook.mailbox_email)
        grouped = group_threads(messages)
        conv_key = resolve_thread_id(secret, thread_id, grouped.keys())
        if conv_key is None:
            raise HTTPException(status_code=404, detail="thread not found")
        msgs = grouped[conv_key]
        projected, unredacted = _project_messages(msgs)
        return {
            "thread_id": thread_id,
            "messages": projected,
            "unredacted_message_count": unredacted,
        }

    @app.post("/v1/search", dependencies=[Depends(_require_bearer)])
    def search(req: SearchRequest = Body(...)) -> dict[str, Any]:
        # Lazy import: search_sasi_faq pulls in sentence-transformers, which
        # is heavy. Tests can monkeypatch the import path if they don't have
        # the [full] extra available.
        from sasi_mcp.mcp_server import search_sasi_faq
        matches = search_sasi_faq(
            config, store, req.question,
            top_k=req.top_k,
            category=req.category,
            include_stale=not req.exclude_stale,
        )
        return {"matches": matches}

    return app


# ---- CLI entrypoint -----------------------------------------------------

def serve_http(
    config: Config,
    *,
    bind: str,
    port: int,
    token: str,
    secret: bytes,
) -> None:
    """Run uvicorn with the FastAPI app. Blocks until SIGINT."""
    try:
        import uvicorn
    except ImportError as exc:
        raise RuntimeError(
            "uvicorn is required. Install the [full] extra: pip install -e '.[full]'"
        ) from exc

    app = create_app(config, token=token, secret=secret)
    _log.info("http_server.start", bind=bind, port=port)
    uvicorn.run(app, host=bind, port=port, log_level="info", access_log=False)
