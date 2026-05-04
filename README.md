# sasi-mcp

Local FAQ corpus + MCP server for the SASI program (`summermed@stanford.edu`).

Reads the shared mailbox via Outlook AppleScript, pairs inbound questions with
outbound replies into Q&A pairs, redacts PII at ingest with Microsoft Presidio
(local-only), and exposes the approved corpus to Claude through an MCP server.

## Why AppleScript?

Stanford Azure AD blocks Microsoft Graph for personal use (AADSTS50105). Reading
a Stanford shared mailbox without a custom Azure app registration leaves
AppleScript-driven Outlook as the only option.

## Pipeline (each step is idempotent)

```
ingest       AppleScript → messages table
redact       Presidio → body_redacted (raw bodies dropped from disk)
extract      LLM → canonical_question + canonical_answer + temporal_anchors
classify     LLM → category
review       Human gate (default = pending until reviewed)
embed        sentence-transformers → embeddings table (approved only)
serve        MCP stdio server: search_sasi_faq, list_categories, get_qa_pair
```

A scheduled `launchd` job runs ingest→redact→extract→classify nightly. Embedding
happens after human review.

## Install

```bash
pip install -e ".[dev]"          # tests only
pip install -e ".[full]"         # full pipeline
```

## CLI

```
python -m sasi_mcp ingest --since 2018-01-01
python -m sasi_mcp redact
python -m sasi_mcp extract
python -m sasi_mcp classify
python -m sasi_mcp review --interactive
python -m sasi_mcp embed
python -m sasi_mcp serve

python -m sasi_mcp audit coverage
python -m sasi_mcp audit staleness
python -m sasi_mcp audit evergreen
python -m sasi_mcp audit queries --sample 20

python -m sasi_mcp mark-stale --pattern "registration deadline"
python -m sasi_mcp supersede <old_qa_id> <new_qa_id>
python -m sasi_mcp reaffirm <qa_id>
```

## Data directory

Everything lives under `~/.sasi-mcp/`:
- `corpus.sqlite` — the corpus (messages, qa_pairs, embeddings, query_log)
- `redaction_audit.jsonl` — per-pair redaction counts (never the matched strings)
- `policy_snapshot.yaml` — current-year facts for staleness cross-check
- `probe_questions.txt` — known FAQ probes for coverage audit

## Status

Bootstrapping. See `~/.claude/plans/here-is-a-draft-pure-feigenbaum.md` for the
full design (phases 0–7).
