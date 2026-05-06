# SASI Desk: integrating sasi-mcp with a draft-reply UI

**Status:** design proposal
**Author:** lchu@stanford.edu
**Date:** 2026-05-05
**Scope:** how a verifier-facing "Desk" UI uses `sasi-mcp` to draft replies to summermed@stanford.edu, send them via SendGrid, and keep the corpus learning from every reply we ship.

---

## 1. Problem statement

We have ~7,500 historical Q&A pairs derived from summermed@stanford.edu in `sasi-mcp`'s local SQLite corpus, plus a working MCP server that can retrieve and ground answers. We want a practical way for SASI staff to use this corpus to draft replies to incoming parent email — without sacrificing PII safety, without locking ourselves into one tool, and with a feedback loop so every reply we ship becomes future training data.

The natural temptation is to bolt this onto our existing Ledger app, which already has a "Desk" pattern, multi-mailbox ingest config, and a familiar verifier workflow. Two infrastructure facts complicate that:

1. **Microsoft Graph cannot read shared mailboxes for our tenant.** Stanford has not granted the privilege; until they do, the only way to read summermed@'s Inbox/Sent Items is the AppleScript bridge running on a Mac with Outlook 16 logged in. Ledger's existing Graph-based ingest (`backend/scripts/jobs/graph_mail_ingest.py`) does not apply here.
2. **PII / FERPA exposure is real.** Summermed@ correspondence contains parent and student names, DOBs, medical-form details, and student IDs. We've already invested in a local redaction pipeline (Presidio + regex pre-pass in `sasi_mcp/redact.py`); pushing raw bodies to a hosted LLM would defeat that work.

These two constraints push us toward a hybrid architecture: the read/extract pipeline must live on a Mac, and the LLM must be local. Send (SendGrid) and the UI can live anywhere with HTTPS.

---

## 2. Architecture overview

```
┌─────────────────────────────────────────────────┐
│  Mac mini (SASI box) — Tailscale-private        │
│                                                 │
│  ┌──────────────┐    ┌──────────────────┐       │
│  │ Outlook 16   │◄──►│ AppleScript      │       │
│  │ (summermed@  │    │ bridge           │       │
│  │  delegate)   │    │ - read folders   │       │
│  └──────────────┘    │ - append Sent    │       │
│                      └────────┬─────────┘       │
│                               │                 │
│                      ┌────────▼─────────┐       │
│                      │ sasi-mcp         │       │
│                      │ - SQLite corpus  │       │
│                      │ - redact/extract │       │
│                      │ - MCP server     │       │
│                      └────────┬─────────┘       │
│                               │                 │
│                      ┌────────▼─────────┐       │
│                      │ Ollama           │       │
│                      │ (llama3.1:8b)    │       │
│                      └──────────────────┘       │
│                                                 │
│  ┌──────────────────────────────────────────┐   │
│  │ append_sent.py — local HTTP listener     │   │
│  │ POST /append_sent  → AppleScript driver  │   │
│  └──────────────────────────────────────────┘   │
└─────────────────────────────────────────────────┘
                         ▲
                         │ Tailscale
                         │
┌────────────────────────▼─────────────────────────┐
│  Ledger app (App Engine Flex)                    │
│                                                  │
│  /api/sasi/threads          ──┐                  │
│  /api/sasi/threads/{id}/draft │ new routes,      │
│  /api/sasi/threads/{id}/send  │ borrow Desk      │
│                              ─┘ chrome           │
│                                                  │
│  SasiDeskPage.tsx — borrows DeskPage primitives  │
└────────────────────────┬─────────────────────────┘
                         │ HTTPS
                         ▼
                    ┌─────────┐
                    │SendGrid │ (authorized to send as summermed@)
                    └─────────┘
```

The Mac mini is the only machine that *must* be on a Mac. Everything else is cloud-portable.

---

## 3. Component responsibilities

### 3.1 Mac mini ("SASI box")

| Component | Source | Responsibility |
|-----------|--------|----------------|
| Outlook 16 | macOS app | Logged in as a user with delegate access to summermed@. Provides the only available read surface. |
| `applescript/list_thread_messages.applescript` | `sasi-mcp` repo | Walks Outlook folders, returns JSON-serialized messages. Already shipping. |
| `applescript/append_sent.applescript` | **new — to build** | Creates a message in summermed@'s Sent Items reflecting a Desk-sent reply. ~30 lines. |
| `sasi_mcp/ingest.py` | existing | Upserts messages, derives Q&A pairs, recomputes threads. |
| `sasi_mcp/redact.py` | existing | Presidio + regex; runs locally, no egress. |
| `sasi_mcp/extract.py` + `classify.py` | existing | Calls local Ollama. |
| `sasi_mcp/mcp_server.py` (FastMCP stdio) | existing | Tools: `search_sasi_faq`, `report_query`, etc. |
| `append_sent_listener.py` | **new — to build** | Tiny FastAPI on `127.0.0.1:8765`, reachable only via Tailscale. Single route `POST /append_sent` shells out to AppleScript. |
| Ollama daemon | `ollama serve` (launchd) | `llama3.1:8b`. ~30–50 tok/s on M-series. |

**Cron cadence (`launchd`):** every 5 minutes, run `sasi-mcp refresh` (ingest --since-last-tick → redact → extract → classify). Cheap on a mini, keeps Desk's view of inbound mail current. The full nightly job runs `embed` and any audit re-runs.

### 3.2 Ledger backend (new SASI surface)

The only Ledger asset we reuse is the *app shell* — auth, deploy, UI primitives, FastAPI route registration. Everything else is parallel:

| Concern | Ledger's existing pattern (do **not** reuse) | New SASI equivalent |
|---------|----------------------------------------------|---------------------|
| Mailbox ingest | `MailboxSource` + Graph job | None — sasi-mcp is authoritative |
| Data model | `Submission` (transactions) | `SasiThread`, `SasiDraft` (email replies) |
| Email send | `email_notifications.render_handoff_email` (verifier handoff) | `sasi_send.send_reply` (RFC822 reply with In-Reply-To/References) |
| AI assist | `bp_generator.py` (Vertex Gemini) | `sasi_reply_service.py` (calls sasi-mcp over Tailscale) |

**New routes:**

- `GET  /api/sasi/threads?status=needs_reply` — list inbound threads awaiting reply.
- `GET  /api/sasi/threads/{conversation_key}` — full message history + recent prior outbound for context.
- `POST /api/sasi/threads/{conversation_key}/draft` — calls sasi-mcp's `search_sasi_faq` over Tailscale, returns `{draft_text, citations: [{qa_id, received_at, score}]}`.
- `POST /api/sasi/threads/{conversation_key}/send` — body: `{final_text, in_reply_to, references}`. Sends via SendGrid, then fires-and-forgets `POST /append_sent` to the Mac mini.
- `POST /api/sasi/threads/{conversation_key}/lock` / `DELETE …/lock` — soft lock (15 min TTL) so two verifiers don't draft the same thread.

### 3.3 SendGrid

Already authorized to send as `summermed@stanford.edu`. The Ledger backend already has a SendGrid client; reuse the same key with a new from-domain entry. The send must include:

- `From: SASI Summer Programs <summermed@stanford.edu>`
- `In-Reply-To: <{parent's Internet Message-Id}>`
- `References: <{full thread chain}>`
- `Reply-To: summermed@stanford.edu`

If we omit `In-Reply-To`/`References` the parent's mail client will not thread our reply, and the conversation looks broken.

---

## 4. Data flow

### 4.1 Ingest path (read)

1. Cron fires `sasi-mcp refresh` every 5 minutes.
2. AppleScript walks Inbox + Sent Items + sub-folders the mailbox owns; the script's `with timeout of 7200 seconds` guards Outlook stalls.
3. Each message row gets upserted into `messages`. **Re-ingest preserves redacted bodies** — `store.upsert_message` checks `body_redacted=1` and skips overwrite.
4. `redact` runs Presidio + regex pre-pass on any unredacted bodies, producing `body_redacted=1` rows.
5. `extract` and `classify` call Ollama on pending pairs.
6. Pairs above the auto-approve threshold (default 0.85) are embedded; the rest stay `pending` for `sasi-mcp review --interactive`.

### 4.2 Draft path (read by Desk)

1. Verifier opens a thread in Desk → frontend calls `GET /api/sasi/threads/{conversation_key}`.
2. Ledger backend reads the parent thread from sasi-mcp's `messages` table (over Tailscale-tunneled HTTP, or direct SQLite read if Ledger is co-located on the mini — see §6.1).
3. Verifier clicks "Draft reply" → `POST /api/sasi/threads/{conversation_key}/draft`.
4. Ledger calls sasi-mcp's `search_sasi_faq(question=<latest_inbound_body>, top_k=5)`; gets back ranked Q&A pairs with `qa_id`, `received_at`, `final_score`.
5. Ledger constructs a draft via a small templated prompt (or, for v1, just lays out the top 3 prior answers and lets the verifier copy-paste). The MCP corpus is the authoritative ground truth — no free-form generation in v1.

### 4.3 Send path (write)

1. Verifier edits draft, clicks Send.
2. `POST /api/sasi/threads/{conversation_key}/send` → Ledger SendGrid client emits the RFC822 message with proper headers.
3. On success, Ledger fires `POST {mac-mini}/append_sent` over Tailscale with the same payload + `received_at = now()`.
4. Mac mini's listener invokes `append_sent.applescript` to materialize the message in summermed@'s Sent Items folder.
5. Within 5 minutes, the next ingest cron picks it up; `derive_qa_pairs` matches it to the inbound parent message via `conversation_key`; extract/classify/embed run on the new pair.

**Failure modes:**
- SendGrid OK, mirror fails: log to `sasi_send_failures`, surface a yellow banner in Desk; replay daily. The reply still went out — only the corpus loop is delayed.
- SendGrid fails: surface error, do not call mirror, do not mark thread sent.

---

## 5. Threading: RFC822 Message-Id support

The current ingest pipeline stores Outlook's internal `EntryID` as `message_id`, not the RFC822 `Message-Id` header. SendGrid replies need the latter.

**Required changes:**

1. **`applescript/list_thread_messages.applescript:serializeMessage`** — read `headers of m` and grep for the `Message-ID:` line; serialize as `internet_message_id`. Fallback to empty string if missing (rare; some old forwards lose it).
2. **`sasi_mcp/store.py:MessageRecord`** — add `internet_message_id: str` field; add column to `messages` schema with a migration.
3. **`sasi_mcp/store.py:upsert_message`** — include in INSERT/UPDATE.
4. **Desk send route** — when assembling the reply, fetch the parent's `internet_message_id` and emit it as `In-Reply-To`. The `References` chain is the parent's `References` (if present) appended with the parent's own `Message-Id`.

Estimated effort: half a day, mostly the AppleScript header parse and the migration.

---

## 6. Mixed-mode coexistence (Desk + native Outlook)

We can't realistically force every staffer into Desk on day one. Native Outlook replies must keep working. The system handles this if we hold to one rule:

> **Outlook Sent Items is the single source of truth for "what we said."** Desk-via-SendGrid replies are mirrored back to Sent Items so both paths converge on the same physical artifact.

Consequences:

- **Outlook-only reply path:** unchanged. Verifier replies in Outlook → message lands in Sent Items → next ingest picks it up → corpus updated.
- **Desk-only reply path:** SendGrid sends + AppleScript mirrors → Sent Items has the message → next ingest picks it up → corpus updated.
- **Auditing:** "show me everything we sent this parent" is one query against `messages WHERE direction='outbound'`, regardless of composition path.
- **Visibility:** Outlook users see Desk replies (because they're in Sent Items). Desk users see Outlook replies after the next 5-minute cron — usually fine, but if a Desk session is open we can run a manual `ingest --limit 20 --since-last-tick` on thread-open to refresh.

### 6.1 Race conditions

Two failure cases:

1. **Two verifiers, same thread, both in Desk.** Soft lock (15 min TTL) keyed on `conversation_key`; second verifier sees "Lawrence is drafting (since 10:42)". Skip for v1 if the team is small.
2. **One verifier in Desk, another in Outlook on the same thread.** Unsolvable without owning Outlook's UI. Best we can do: when Desk opens a thread, peek at the parent's `last_received_at`; if a new outbound message has appeared in Sent Items since that timestamp, surface "New reply from Outlook detected — refresh thread." Add only if collision pain is real.

---

## 7. PII / privacy boundary

| Data | Lives where | Crosses Tailscale? | Crosses public internet? |
|------|-------------|-------------------|--------------------------|
| Raw email bodies (inbound) | Mac mini SQLite only | No | No |
| Redacted bodies (post-Presidio) | Mac mini SQLite | Yes (when Desk requests draft context) | No |
| Q&A pairs (canonical) | Mac mini SQLite | Yes | No |
| Outbound reply final text | Mac mini SQLite + Outlook Sent Items | Yes | **Yes** (SendGrid → recipient) |
| LLM prompts/responses | Local Ollama only | No | No |

The only public-internet egress of student/parent-identifiable content is the outbound reply itself, which is by definition going to the recipient anyway. All upstream processing — extraction, classification, retrieval — runs on the Mac. This is the privacy story we promised when we chose Presidio + local Ollama.

**One follow-up:** when Desk requests draft context from the Mac mini, the response should be redacted bodies only. Verify the API never returns `body_text` (raw); only `body_redacted` (post-Presidio).

---

## 8. Phasing

**Phase A — corpus only (complete 2026-05-06).**
- Extract+classify pipeline finished on the 7,552-pair corpus (7,036 produced
  usable canonical questions, 7,033 classified).
- Three-phase quality pass landed: cheap pair-quality filter (464 demoted,
  ~50% → ~87% sample quality), per-category leaf-mode HDBSCAN for
  contradiction detection (1 giant cluster → 128 tight clusters), domain-
  specific medical-forms probes (10 of 11 cleared the similarity floor).
- Final state: 4,312 approved + embedded, 3,240 pending, 59 tests passing.
- Detailed results: `docs/corpus-quality-2026-05-06.md`.
- Still open: 3,240 pending pairs need either a lower auto-approve threshold
  or a session with `sasi-mcp review --interactive`. The corpus is usable
  for retrieval today but should not be considered final until that queue
  is triaged.

**Phase B — Desk MVP, read-only.**
- Build `SasiDeskPage.tsx` that lists threads and lets verifiers see suggested prior answers via `search_sasi_faq`. **No send capability yet.**
- Verifier reads suggestion in Desk, copies into native Outlook to send. Lets us validate retrieval quality without committing to the SendGrid + mirror plumbing.
- Effort: ~3–5 days.

**Phase C — Desk send via SendGrid.**
- Add `internet_message_id` to ingest schema + migration.
- Build `append_sent.applescript` + Mac mini HTTP listener.
- Wire `POST /api/sasi/threads/{id}/send` in Ledger.
- Ship to one verifier first, watch the corpus loop close (sent reply re-ingested → new Q&A pair → re-embedded).
- Effort: ~1 week.

**Phase D — hardening.**
- Soft locks for concurrent drafting.
- "New reply from Outlook detected" banner.
- Mirror retry queue.
- Per-category clustering for contradiction detection (deferred work item #2 in the existing project plan).

**Phase E (optional) — generation.**
- Today the verifier sees prior answers and writes their own reply. Once we trust the corpus, we add a prompted-LLM step on the Mac mini that drafts a reply *grounded* in the top-k retrieved pairs. Verifier still edits and approves before send.
- Gate on: ≥6 weeks of Phase C running cleanly, contradiction rate <2% on per-category clustering audit, and explicit go-ahead from program leadership.

---

## 9. Open questions

1. **Where does Ledger's backend live relative to the mini?** Two options:
   - (a) Ledger on App Engine Flex (current), Mac mini reachable via Tailscale. Cleanest separation; mini does inference, cloud does UI.
   - (b) Co-locate a slim Ledger-derived FastAPI on the mini itself, behind Tailscale. Skips the cross-network hop; loses cloud auth/logging integration. Worth piloting if Tailscale latency is painful (>200ms p95).
2. **Should `sasi-mcp` expose an HTTP-mode server for Desk to call?** Today MCP is stdio-only via FastMCP. Either we add an HTTP transport to `sasi-mcp serve`, or Ledger's backend shells out to `python -m sasi_mcp ...` per request. The HTTP path is cleaner and ~50 lines.
3. **Sub-folder coverage for medical-forms questions.** The existing project plan has this as deferred work item #4 (subject stripping + medical-forms probes). Whether we ship Desk before or after that depends on retrieval quality measured against medical-forms probes specifically — the highest-pain verifier topic.
4. **Authoritative reply attribution.** When SendGrid sends a Desk-composed reply, should the Sent Items mirror show the verifier's name as sender, or summermed@? Probably summermed@ for thread continuity; verifier identity goes in an `X-SASI-Verifier:` header for audit. Decide before shipping Phase C.

---

## 10. References

- `sasi-mcp/applescript/list_thread_messages.applescript:1` — current Outlook bridge, AppleScript walker.
- `sasi-mcp/applescript/list_account_folders.applescript:1` — folder discovery for ingest.
- `sasi-mcp/sasi_mcp/ingest.py:27` — `ingest()` driver.
- `sasi-mcp/sasi_mcp/store.py` — `MessageRecord`, `upsert_message`, redacted-body preservation.
- `sasi-mcp/sasi_mcp/redact.py` — Presidio engine + regex pre-pass.
- `sasi-mcp/sasi_mcp/mcp_server.py` — `search_sasi_faq` MCP tool.
- `ledger/backend/app/models/submission.py:59` — `Submission` lifecycle (the pattern we're paralleling for `SasiThread`).
- `ledger/backend/app/services/bp_generator.py:1` — Vertex Gemini draft pattern (the architectural sibling of `sasi_reply_service`).
- `ledger/frontend/src/pages/DeskPage.tsx:1` — UI primitives to borrow for `SasiDeskPage.tsx`.
- `ledger/backend/scripts/jobs/graph_mail_ingest.py:1` — reference only; **not used** for summermed@ because Graph is denied for shared mailboxes.
