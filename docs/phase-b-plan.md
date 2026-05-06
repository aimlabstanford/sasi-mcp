# Phase B plan — read-only Desk MVP

**Status:** proposed
**Author:** lchu@stanford.edu
**Date:** 2026-05-06
**Scope (locked by reviewer):** thread list, thread detail, retrieval via
`search_sasi_faq`, `SasiDeskPage.tsx` skeleton. Only redacted content crosses
the boundary. **Out of scope:** SendGrid, draft persistence, soft locks,
mirror endpoint, RFC822 Message-ID, write paths of any kind.

---

## TL;DR

Add a small HTTP server to `sasi-mcp` (the FastMCP stdio server stays — this
is a sibling, not a replacement) with three GET routes plus one POST search.
Ledger backend gets a thin client + a thin router that proxies to sasi-mcp.
Ledger frontend gets a single `SasiDeskPage.tsx` that lists threads, renders
one, and shows top-k suggested prior answers from the corpus. Verifier reads
in Desk, replies from native Outlook (no send plumbing yet).

The single biggest blocker is **how Ledger's App Engine Flex backend reaches
the Mac mini** running sasi-mcp. That question must be answered before any
Ledger-side code goes in. The sasi-mcp HTTP server itself can be built
independently and tested locally — that's where I'd start.

---

## 1. Data flow

```
Verifier opens Desk → Ledger frontend  ─── HTTPS ──→  Ledger backend
                                                          │
                                                          │ Tailscale (or
                                                          │ Cloudflare Tunnel
                                                          │ — see §6)
                                                          ▼
                                                     Mac mini
                                                     ┌──────────────────┐
                                                     │ sasi-mcp         │
                                                     │ http-serve       │
                                                     │ :8765, bearer    │
                                                     │ ─────────────    │
                                                     │ GET /threads     │
                                                     │ GET /threads/:k  │
                                                     │ POST /search     │
                                                     │ ─────────────    │
                                                     │ Store (sqlite)   │
                                                     │ Embeddings cache │
                                                     │ (Ollama: not     │
                                                     │  needed for B —  │
                                                     │  retrieval only) │
                                                     └──────────────────┘
```

Important property: **no PII payload egresses**. The HTTP server returns
only redacted bodies (`body_redacted=1` rows; raw is never serialized).
Q&A canonical text was already redacted upstream by Presidio. The token
auth is defense in depth — the network layer (Tailscale ACL or
Cloudflare service token) is the primary boundary.

---

## 2. API shape

**Privacy rule (locked):** only redacted bodies and canonical Q&A cross
the boundary. The contract below deliberately omits `subject`,
`sender_email`, `sender_name`, `external_party`, raw `message_id`, and
the raw `conversation_key`. Threads are addressed by an opaque
`thread_id = HMAC-SHA256(server_secret, conversation_key)[:16]`. The
HMAC is deterministic so reverse lookup on `GET /threads/{thread_id}`
is stateless — the server re-hashes every conversation_key on demand
and matches.

If we want subject in the UI later, we add a subject-redaction pass
upstream (or explicitly relax the privacy boundary) before adding it
to the response. Same for sender display names.

### `GET /v1/healthz`

No auth. For monitoring + Cloudflare Tunnel health checks.

Response:
```json
{ "status": "ok", "schema_version": 2, "embeddings": 4312 }
```

### `GET /v1/threads`

Bearer-auth required. Query params:
- `status` ∈ {`needs_reply` (default), `all`} — see §5 for the heuristic.
- `limit` int, default 50, max 200.
- `since` ISO date, optional — only threads with `last_received_at ≥ since`.

Response:
```json
{
  "threads": [
    {
      "thread_id": "9f3a82bc1d...",
      "first_received_at": "2026-04-12T...",
      "last_received_at": "2026-05-05T...",
      "message_count": 4,
      "has_outbound": true,
      "latest_direction": "inbound",
      "redacted_preview": "Hello, my child <PERSON_1> needs to know..."
    }
  ],
  "total_returned": 50
}
```

`redacted_preview` is the first 200 chars of the latest message's
`body_text` IF that message has `body_redacted=1`. If the latest
message is unredacted (e.g. just-ingested, before the next redact
tick), `redacted_preview` is empty and the response includes a
top-level `pending_redaction_count` so the frontend can show "n
threads have unredacted recent messages — refresh shortly."

### `GET /v1/threads/{thread_id}`

Bearer-auth required. Returns 404 if the thread_id doesn't resolve.

Response:
```json
{
  "thread_id": "9f3a82bc1d...",
  "messages": [
    {
      "message_index": 0,
      "direction": "inbound",
      "received_at": "2026-04-12T...",
      "redacted_body": "Hello, my child <PERSON_1> is..."
    },
    {
      "message_index": 1,
      "direction": "outbound",
      "received_at": "2026-04-13T...",
      "redacted_body": ""
    }
  ]
}
```

`message_index` is 0-based within the thread (after sorting by
`received_at`). It's redaction-safe (just an integer) and gives the
frontend a stable cursor for scroll/highlight.

**`redacted_body` rule, hard-enforced:** the serializer reads from
`messages.body_text` *only when `messages.body_redacted = 1`*. Raw
rows return an empty string. The response also includes
`unredacted_message_count` so the frontend can render a "this thread
has n unredacted messages, refresh shortly" notice instead of
silently hiding them.

### `POST /v1/search`

Bearer-auth required.

Request:
```json
{
  "question": "When are medical forms due?",
  "top_k": 5,
  "exclude_stale": true,
  "category": null
}
```

`exclude_stale` defaults to `true`. The underlying
`search_sasi_faq` already filters to `review_status='approved'`
unconditionally, so search is approved-only by design — the request
schema doesn't expose a knob to relax that.

Response:
```json
{
  "matches": [
    {
      "qa_id": "abcd1234...",
      "canonical_question": "When must completed medical forms be ...",
      "canonical_answer": "Medical forms are due two weeks before ...",
      "received_year": 2025,
      "category": "health_safety",
      "score": 0.78,
      "cosine": 0.82,
      "recency": 0.94,
      "evergreen_score": 0.6,
      "stale_reason": null
    }
  ]
}
```

Canonical Q&A are already Presidio-redacted upstream. `qa_id` is a
SHA-derived opaque hash (no PII). `received_year` is exposed instead
of the full ISO `received_at` — the year is enough for the verifier
to gauge recency and avoids emitting an exact date that could be
correlated to an inbound thread.

This wraps the existing `search_sasi_faq(config, store, question,
top_k, category, include_stale)` from `mcp_server.py`. Phase B does
not add new retrieval code; it exposes the existing function over
HTTP with the privacy projection above.

---

## 3. Files / modules to touch

### `sasi-mcp` repo

```
sasi_mcp/
  http_server.py         ← NEW: FastAPI app, four routes (healthz,
                            threads list, thread detail, search),
                            bearer-token middleware, deterministic
                            HMAC thread_id helpers. ~250 LOC.
  __main__.py            ← +1 subcommand: `sasi-mcp http-serve
                            [--bind 127.0.0.1] [--port 8765]
                            [--token-file PATH] [--secret-file PATH]`.
                            ~20 LOC.
tests/
  test_http_server.py    ← NEW: FastAPI TestClient covering all four
                            routes, bearer-auth rejection, raw-body-
                            never-leaks property, opaque-thread_id
                            round-trip, and 404 on unknown thread_id.
                            ~250 LOC.
pyproject.toml           ← Add `fastapi` and `uvicorn[standard]` to
                            the [full] extra.
```

Note: I considered extracting search into a new `search.py`, but
`search_sasi_faq` in `mcp_server.py` already takes `(config, store,
question, …)` so the HTTP layer can call it directly with no
refactor. Keeping the existing module structure.

Estimated ~500 LOC total in sasi-mcp, isolated to new files plus a
small CLI plumbing addition. Doesn't touch ingest, redact, extract,
classify, embed, or the existing MCP stdio surface.

### `ledger` repo

```
backend/app/services/
  sasi_client.py         ← NEW: typed httpx client for the SASI box.
                            Reads `SASI_MCP_URL` and `SASI_MCP_TOKEN`
                            from env. ~80 LOC + retry/timeout logic.

backend/app/api/
  sasi.py                ← NEW: FastAPI router exposing
                            /api/sasi/threads, /api/sasi/threads/{key},
                            /api/sasi/search. Phase B is mostly thin
                            pass-through; future phases add policy,
                            audit, and locks here. ~120 LOC.

backend/app/main.py
  (or wherever routers   ← +1 line: include_router(sasi.router,
   register)               prefix="/api/sasi").

frontend/src/pages/
  SasiDeskPage.tsx       ← NEW: 3-pane layout — thread list (left),
                            selected-thread reader (middle), suggested-
                            answers panel (right). v1 ~250 LOC, lifts
                            primitives from DeskPage.tsx. Read-only:
                            no input field, no send button.

frontend/src/api/
  sasi.ts                ← NEW: typed fetch hooks (useThreads,
                            useThread, useSearch). ~80 LOC.

frontend/src/router/     ← +1 route: `/sasi/desk` → SasiDeskPage. Behind
                            existing auth wrapper.
```

Estimated ~600 LOC in Ledger, isolated to one new router + one new page.

### Auth boundary

Phase B uses **Ledger's existing user auth as the only user-facing
gate.** Anyone authorized to see DeskPage can see SasiDeskPage. We do
not introduce a new role yet — the integration is read-only and only
returns redacted content, so the existing Desk authorization scope is
sufficient. (A future phase that adds reply-send needs a tighter role.)

The Ledger ↔ sasi-mcp bearer token is service-to-service, never seen by
the browser.

---

## 4. Smallest end-to-end slice

Order of merge:

1. **PR a (sasi-mcp side)** — http_server + search.py refactor + tests.
   Mergeable independently of Ledger. Validated locally with curl. No
   reachability question needed.
2. **PR b (Ledger backend)** — sasi_client + sasi router + integration
   test that mocks the SASI HTTP server. **Blocked on reachability
   decision (§6) before deploy.** Code can be reviewed and merged behind
   a feature flag.
3. **PR c (Ledger frontend)** — SasiDeskPage skeleton. v1 just thread
   list. Mergeable behind the same feature flag.
4. **Reachability rollout** — once §6 is decided, point Ledger at the
   live SASI box and flip the flag for one verifier. Watch p95 latency,
   error rate, and "redacted body" warnings for a week.
5. **Iterate** — the suggested-answers panel, search quality knobs, the
   thread reader's UX details, and the second/third feature requests
   from the pilot verifier.

PR a is mergeable today. PR b/c are mergeable as soon as the Ledger half
has a recipe for "talk to a Tailscale-only host."

---

## 5. Thread-list semantics: what does "needs_reply" mean?

The DB doesn't have a `replied` flag — we have to derive it. Proposed
heuristic for `status=needs_reply`:

> A thread needs a reply if its latest message is `direction='inbound'`
> AND there's no outbound message with `received_at` greater than that
> latest-inbound timestamp.

This is approximate — a verifier may have replied via Outlook directly
since the last ingest tick, or replied to an *earlier* message in the
thread without addressing the latest. v1 ships the heuristic; v2 can
add a "mark thread handled" button that writes a row to a new
`thread_dispositions` table.

A verifier sweep + my read of the design doc suggests this heuristic is
right ~85-90% of the time, which is good enough for the MVP.

---

## 6. Open questions / blockers

### B1 — reachability (decided)

**Decision:** Cloudflare Tunnel + service token for the MVP.

**Code constraint that follows:** the FastAPI app and CLI must stay
transport-agnostic. `http_server.py` only knows about a bind address
and a bearer token; it does not import or reference Cloudflare. All
tunnel setup (cloudflared daemon on the mini, ingress config, service
tokens) is operational and lives outside the codebase. If we swap to
Tailscale or another path later, no app changes should be required —
only the deploy recipe.

### B2 — Mac mini procurement / siting

The Mac mini that hosts sasi-mcp in production needs to be a stable
always-on machine logged into Outlook with delegate access to summermed@.
Today this is the user's laptop. Where will it live? Who restarts
Outlook when it gets stuck? Operational, not a code blocker.

### B3 — Latency budget

Cold-start search call on M-series: query encode ~200-500ms (sentence-
transformers loads `all-mpnet-base-v2` lazily); cosine over 4,312 vectors
~50ms. Plus Tailscale/Cloudflare RTT ~30-100ms. Total ~300-700ms p95 for
the first call, ~80-150ms steady-state once embeddings are cached in
process memory. **Mitigation:** http_server should warm the embeddings
cache and the SentenceTransformer model on startup, not on first
request. Already planned in §3.

### B4 — Thread list shape

Surfaced for reviewer agreement before frontend work starts:
- Default sort: `last_received_at DESC` ✓ obvious
- Default filter: `status=needs_reply` ✓ proposed in §5
- Show: subject, external party, last-received-at, message count,
  inbound/outbound badge for the latest message ✓ proposed in §2
- Hide threads where the latest message is from an automated sender
  (`@noreply.*`, `@notifications.*`) — TBD; depends on whether the
  corpus has them.

### B5 — Stale gating in retrieval (decided)

`exclude_stale=true` is the default. Approved-only is also enforced
by the underlying `search_sasi_faq` and not exposed as a request
knob. (Including stale results would surface "we used to say it
costs $X" answers, which is worse than no answer.)

### B6 — Pending pairs and coverage gaps

The reviewer's queued follow-up. **My recommendation:** ship Phase B
sasi-mcp side first (PR a above) so we can measure retrieval quality
*against the actual MVP UI* before deciding whether to drop the
auto-approve threshold or run a manual review pass. If verifiers find
the top-3 results unhelpful in real use, that's a stronger signal than
the 8-pair sample we did during the quality pass. The "typical day"
and "personal laptop" coverage gaps are small enough that we can write
canonical answers by hand once a verifier confirms the gaps matter
operationally.

---

## 7. What I propose to do next, by default

If the reviewer concurs:

1. **Now:** start PR a (sasi-mcp HTTP server). No blocker, no Ledger
   touch required. ~1-2 days.
2. **In parallel:** ask the reviewer to pick from §6 / B1.
3. **Once B1 is decided:** start PR b (Ledger backend client + router).
4. **Then:** PR c (Ledger frontend skeleton).
5. **Defer:** pending-pairs review pass and coverage-gap fill — pick up
   after MVP is in front of one verifier and we have signal on whether
   retrieval quality is the bottleneck.

Pause for confirmation before I start PR a if the reviewer wants to
adjust scope, contract shape, or B1 first.
