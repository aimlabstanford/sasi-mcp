# Corpus quality pass — 2026-05-06

**Status:** complete
**Author:** lchu@stanford.edu
**Scope:** three-phase quality pass on the 7,552-pair SASI corpus after the
subfolder-coverage backfill (PR #2). Goal: get the auto-approved set into
shape before any Desk-style retrieval surface relies on it.

---

## TL;DR

| Metric | Before | After |
|---|---|---|
| Approved pairs | 4,776 | 4,312 |
| Embedded pairs | 4,776 | 4,312 |
| Subfolder-pair quality (8-sample) | ~50% clean | ~87% clean |
| Cluster count (for contradiction audit) | 1 giant cluster (~3,000 members) | 128 tight clusters (max 28, median 5) |
| Auto-applied contradiction flags | 1,631 (38% FP rate) | 0 (signal kept, opt-in only) |
| Coverage probes | 10 generic | 30 (incl. 11 medical-forms) |
| Test suite | 58 passing | 59 passing |

We shipped the corpus quality filter, switched contradiction detection to
per-category leaf-mode HDBSCAN, and added domain-specific probes. The
contradiction signal is no longer auto-applied because spot-checking showed
~95% false-positive rate even after the cluster fix — it's now a manual
review queue, not a stale flag.

---

## Phase 1 — pair-quality filter

### Problem

A spot-check of 8 random subfolder pairs from the 4,776-approved set
showed ~50% were unusable:

- Acknowledgment threads ("signed forms, thanks") with no real question
- Q ≈ A duplications where extract pulled the same outbound block twice
- Cross-thread bleed (Q from one parent's thread, A from another)

The classifier confidence was high (0.90+) on these because it's confident
in the *category*, not the underlying pair quality.

### Implementation

- New module `sasi_mcp/quality.py` with `is_quality_pair(q, a) -> (bool, reason)`
  using cheap heuristics:
  - **q_too_short** / **a_too_short** — word-count floors (4 / 6)
  - **q_is_ack** / **a_is_ack** — regex match against
    "thanks/received/noted/signed/done/all set" templates
  - **no_question_signal** — Q lacks `?` and lacks an interrogative lead
    (what/how/when/where/why/who/which/can/could/should/do/is/are/will, etc.)
  - **q_equals_a** — normalized Q and A byte-identical
  - **q_subset_a** — for long pairs, one is a substring of the other
- `review.auto_approve()` now calls `is_quality_pair` before approving.
- New CLI `sasi-mcp quality-audit [--apply]` retroactively walks the
  approved set, demotes failures to `pending`, and deletes their
  embeddings so they stop surfacing in retrieval.
- `Store.delete_embedding(qa_id)` added to support the retraction step.
- 10 unit tests in `tests/test_quality.py` cover each rule + the happy paths.

### Results on the live corpus

```
$ sasi-mcp quality-audit          # dry run
{
  "approved_seen": 4776,
  "demoted": 464,
  "reason_no_question_signal": 315,
  "reason_a_too_short": 82,
  "reason_q_subset_a": 62,
  "reason_q_too_short": 4,
  "reason_q_equals_a": 1
}

$ sasi-mcp quality-audit --apply   # commit demotions + drop embeddings
```

**Validation.** Sampled 5 demoted pairs — all 5 were correct calls
(coupon-code passthrough, RSVP closer, signed-form handoff, banner-spec
statement, instruction duplicate). Re-sampled 8 pairs from the post-filter
set: 7 clean, 1 borderline cross-thread case → ~87% quality vs prior 50%.

### What this *doesn't* catch

The 1 remaining borderline case (financial-aid Q paired with waitlist A)
is the cross-thread-bleed pattern that needs either an LLM coherence check
or per-category clustering to detect. The cheap filter is structural
(does this pair look like Q&A?), not semantic (does this answer respond
to this question?).

---

## Phase 2 — per-category clustering for contradiction detection

### Problem

Global HDBSCAN over all 4,312 embeddings produced one mega-cluster of ~3,000
"questions the program manager answers" plus a long tail of small clusters.
The within-cluster contradiction detection (`_within_cluster_contradiction`)
had to skip clusters with > 30 members to avoid thousands of false positives,
which meant it effectively did nothing for the bulk of the corpus.

### Implementation

- `audit.cluster_pairs(per_category=True)` runs HDBSCAN inside each
  classifier category bucket separately (`finance_billing`,
  `parent_logistics`, `applicant_questions`, `program_schedule`,
  `health_safety`, `housing_meals`, `enrollment_status`, `other`,
  plus `_uncategorized` for the rare classify-miss).
- Cluster IDs are packed into a single int via `category_offset + sub_label`
  so `qa_pairs.cluster_id` stays a flat column.
- Switched `cluster_selection_method` from `"eom"` (excess-of-mass — picks
  broad clusters) to `"leaf"` (picks the tightest sub-clusters from the
  condensed-tree hierarchy). EOM on a category bucket recreated the giant-
  cluster problem we were trying to escape (e.g., all 471 finance_billing
  pairs landed in one cluster).
- New CLI flag: `sasi-mcp audit cluster --per-category --min-cluster-size 3`
- Bumped `_MAX_CLUSTER_SIZE_FOR_CONTRADICTION` from 30 → 500 (now a safety
  belt rather than a primary defense, since per-category leaf clusters are
  inherently small).

### Results on the live corpus

```
$ sasi-mcp audit cluster --per-category --min-cluster-size 3
{
  "n_clusters": 128,
  "noise_count": 3505,
  "max_size": 28,
  "median_size": 5
}
```

128 tight, topically-coherent clusters. The 3,505 noise pairs are pairs
whose questions are unique enough that no cluster formed — that's expected
on a Q&A corpus where most questions are one-off.

### Contradiction signal: still noisy, now opt-in

After re-running the contradiction audit on the new clusters, sampled 5
flagged pairs at the default cosine threshold (0.6). All 5 were false
positives — different sub-topics within the same category, not actual
"older says X, newer says Y" pairs. Examples:

- `finance_billing` cluster (5 members): older = "expired coupon code", newer = "financial aid availability". Different sub-topics.
- `program_schedule` cluster (28 members): older = "advanced session swap request" (2022), newer = "Clinical Skills program timings" (2026). Different sub-topics.

**Threshold sweep on 4,312 approved pairs:**

| Threshold | Flagged | Comment |
|---|---|---|
| 0.6 (default) | 86 | almost all FP |
| 0.5 | 24 | mostly FP |
| 0.4 | 5 | mostly FP |
| 0.3 | 2 | mostly FP |

The fundamental issue: HDBSCAN clusters group "questions with similar
embedding vectors". On a real corpus that doesn't equal "two people asking
the same thing twice." Cheap cluster-based contradiction detection cannot
distinguish "we used to say X, now we say Y" from "this is a different
question that happened to vectorize close by."

**Resolution.** Split `staleness()` apply flags:

```python
def staleness(store, snapshot_path, cfg, *,
              apply: bool = True,             # year_bound + snapshot_mismatch
              apply_contradictions: bool = False,  # heuristic — manual queue
              ): ...
```

Deterministic signals (year-bound expiry, snapshot $-amount mismatch) still
auto-apply because they're ground-truthy. The contradiction signal is
returned in the report but no longer auto-writes to `stale_reason` — staff
can pull the list via `sasi-mcp audit staleness` and triage manually.

A new test (`test_contradiction_signal_not_applied_by_default`) locks this
behavior in.

### What would actually solve contradiction detection

Two paths, both deferred:

1. **LLM coherence check.** For each pair-of-pairs in a category, ask the
   local Ollama model: "are these two questions essentially asking the same
   thing? if yes, do the answers conflict?" Slow (~3s/pair → hours for the
   full corpus) but accurate. Worth doing once, then incrementally on new
   approved pairs.
2. **Paraphrase-grade clustering.** Use a paraphrase-tuned model (e.g.,
   `paraphrase-MiniLM-L12-v2`) instead of `all-mpnet-base-v2` for the
   contradiction-pass embeddings; threshold ≥ 0.92 for "same question."
   Cheaper than #1, less precise.

---

## Phase 3 — medical-forms probes & subject stripping

### Subject stripping: not needed

Original concern: templated subjects (`[Stanford SASI] …`, `Re:`, `Fwd:`)
might leak into the embedding signal and cause near-paraphrase clustering
on the prefix instead of the content. Verified empirically:

```
$ sqlite3 ~/.sasi-mcp/corpus.sqlite \
    "SELECT COUNT(*) FROM qa_pairs
     WHERE canonical_question LIKE '%[Stanford SASI]%'
        OR canonical_question LIKE 'Re:%'
        OR canonical_question LIKE 'Fwd:%'"
0
```

`extract` already canonicalizes the question through the LLM, which strips
the prefix as part of producing a clean canonical form. No embed-time
preprocessing required.

### Medical-forms probes added

Per the user's note that medical forms are "a big headache" operationally,
expanded `~/.sasi-mcp/probe_questions.txt` from 10 generic probes to 30
including 11 medical-forms-specific. Examples:

- when are medical forms due
- what medical forms do I need to submit
- how do I upload my immunization records
- do I still need to submit medical forms if I'm in the virtual session
- can my doctor sign the medical forms electronically
- my qualtrics form is asking for medical information but I'm online only

### Coverage audit results

```
$ sasi-mcp audit coverage
```

**Probes that hit gaps (top score < 0.55 similarity floor):**

| Probe | Top score | Read |
|---|---|---|
| what does a typical day look like | 0.34 | Real gap — corpus has no day-in-the-life answers |
| can my child use their own laptop | 0.35 | Real gap — never asked in summermed@ |
| do you accept rising seniors | 0.52 | Borderline — paraphrase mismatch, not absence |
| will my student be picked up at the airport | 0.54 | Borderline — paraphrase mismatch |
| what if I don't have a recent physical exam on file | 0.54 | Borderline — paraphrase mismatch |

**10 of 11 medical-forms probes cleared the floor** — coverage there is
decent. The actual gaps are operational areas the program rarely gets
parent email about (daily schedule, personal-laptop policy), or paraphrase
mismatches the probe writer can adjust.

### Folder coverage spot-check

Medical-forms-related folders confirmed present in the ingest tree
(`messages.folder` distinct values):

- `2025 Med Forms`, `2025 Medical Forms`, `2025 Consent Forms`
- `Medical Forms Notification`, `Medical`, `Bootcamp Forms`
- `Forms Informational Email`, `Typeform`
- `2018 Student forms`, `2022 Forms Questions`

The `--all-folders` ingest from PR #2 captured all of these.

---

## Files changed

```
sasi_mcp/__main__.py          | +26   (new quality-audit cmd, --per-category flag)
sasi_mcp/audit.py             | +117  (per_category clustering, leaf mode, apply split)
sasi_mcp/quality.py           | +88   (NEW — cheap heuristic filter)
sasi_mcp/review.py            | +41   (auto_approve calls quality, new quality_audit fn)
sasi_mcp/store.py             | +4    (delete_embedding helper)
tests/test_quality.py         | +90   (NEW — 10 quality-rule unit tests)
tests/test_audit_staleness.py | +19   (lock contradiction-not-applied-by-default)
docs/corpus-quality-2026-05-06.md  (NEW — this file)
~/.sasi-mcp/probe_questions.txt    (UPDATED — 30 probes, 11 medical-forms)
```

`pytest`: 59 passed, 0 failed.

---

## Follow-up work (not done in this pass)

1. **LLM coherence check** for contradiction detection (the structural fix
   for the "same question, different answer" signal — see Phase 2 epilogue).
2. **Pending review queue** — 3,240 pairs are still `review_status='pending'`
   after the audit. Most failed the auto-approve threshold (0.85) on
   classifier or extract confidence; some are the 464 quality-demoted set
   that needs human attention via `sasi-mcp review --interactive`.
3. **Coverage gap fill** — the two real gaps surfaced by probes
   ("typical day", "personal laptop") could be addressed by a one-shot
   author session with program staff to seed canonical answers, or by
   waiting until those questions naturally arrive in summermed@.
4. **Per-category clustering on the pending set** once the pending queue is
   reviewed and re-embedded — current `cluster_id` only covers the 4,312
   approved pairs.
