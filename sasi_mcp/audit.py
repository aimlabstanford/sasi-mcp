"""Coverage / staleness / evergreen audits.

These are the Phase-7 self-checks that keep the corpus honest.

* `coverage`: thread completeness, topic clusters (HDBSCAN over canonical
  embeddings), known-question probe set.
* `staleness`: year-bound expiry, snapshot cross-check, within-cluster
  contradiction (older answers whose vectors diverge from the newest in
  the same cluster).
* `evergreen`: pairs that recur across ≥3 distinct calendar years inside
  a tight cluster get a positive `evergreen_score`.
* `queries`: sample recent MCP queries and ask the user to mark them
  good/bad; bad results are auto-requeued for review.
"""

from __future__ import annotations

import math
import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from sasi_mcp.config import StalenessConfig
from sasi_mcp.logger import get_logger
from sasi_mcp.store import QAPair, Store, unpack_vector

_log = get_logger("sasi_mcp.audit")


# --- coverage --------------------------------------------------------------


@dataclass(slots=True)
class CoverageReport:
    threads_total: int
    threads_with_outbound: int
    threads_with_qa: int
    pairs_by_year: dict[int, int]
    cluster_summaries: list[dict[str, Any]]
    probe_gaps: list[dict[str, Any]]


def coverage(store: Store, probe_path: Path | None = None,
             similarity_floor: float = 0.55) -> CoverageReport:
    messages = store.list_messages()
    threads_total = len({m.conversation_id or m.subject for m in messages})
    threads_with_outbound = len({
        m.conversation_id or m.subject for m in messages if m.direction == "outbound"
    })
    qa_pairs = store.list_qa_pairs()
    threads_with_qa = len({p.conversation_key for p in qa_pairs if p.canonical_question})
    pairs_by_year: Counter[int] = Counter()
    for p in qa_pairs:
        if p.received_at and len(p.received_at) >= 4:
            try:
                pairs_by_year[int(p.received_at[:4])] += 1
            except ValueError:
                continue
    cluster_summaries = _cluster_summaries(store)
    probe_gaps: list[dict[str, Any]] = []
    if probe_path and probe_path.exists():
        probe_gaps = _probe_gaps(store, probe_path, similarity_floor)
    return CoverageReport(
        threads_total=threads_total,
        threads_with_outbound=threads_with_outbound,
        threads_with_qa=threads_with_qa,
        pairs_by_year=dict(pairs_by_year),
        cluster_summaries=cluster_summaries,
        probe_gaps=probe_gaps,
    )


def _cluster_summaries(store: Store) -> list[dict[str, Any]]:
    pairs = {p.qa_id: p for p in store.list_qa_pairs(include_stale=True)}
    by_cluster: dict[int, list[QAPair]] = defaultdict(list)
    for p in pairs.values():
        if p.cluster_id is not None:
            by_cluster[int(p.cluster_id)].append(p)
    out: list[dict[str, Any]] = []
    for cid, members in sorted(by_cluster.items()):
        approved = sum(1 for m in members if m.review_status == "approved")
        stale = sum(1 for m in members if m.stale_reason and not m.user_overridden_stale)
        last_seen = max((m.received_at for m in members), default="")
        out.append({
            "cluster_id": cid,
            "n": len(members),
            "approved": approved,
            "stale": stale,
            "last_seen": last_seen[:7],
        })
    return out


def _probe_gaps(store: Store, probe_path: Path, floor: float) -> list[dict[str, Any]]:
    """Run each probe through the search path; flag those with no hit ≥ floor."""
    try:
        from sasi_mcp.embed import cosine_topk, encode_query
    except RuntimeError:
        return []
    candidates = store.list_embeddings()
    if not candidates:
        return [{"probe": line.strip(), "top_score": None}
                for line in probe_path.read_text().splitlines() if line.strip()]
    out: list[dict[str, Any]] = []
    for line in probe_path.read_text().splitlines():
        probe = line.strip()
        if not probe:
            continue
        # Embedding model name read from the first stored vector.
        model = candidates[0][2]
        qv = encode_query(model, probe)
        top = cosine_topk(qv, candidates, 1)
        top_score = top[0].score if top else 0.0
        if top_score < floor:
            out.append({"probe": probe, "top_score": round(top_score, 3)})
    return out


# --- staleness -------------------------------------------------------------


@dataclass(slots=True)
class StalenessReport:
    year_bound_expired: list[str]
    snapshot_mismatch: list[str]
    contradicted_by_newer: list[str]


_RE_DOLLAR = re.compile(r"\$([\d,]+)(?:\.\d+)?")


def _load_snapshot(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return yaml.safe_load(path.read_text()) or {}
    except yaml.YAMLError as exc:
        _log.warning("audit.snapshot_parse_failed", error=str(exc))
        return {}


def _snapshot_dollar_amounts(snapshot: dict[str, Any]) -> set[int]:
    out: set[int] = set()
    for k, v in snapshot.items():
        if k.endswith("_usd") and isinstance(v, (int, float)):
            out.add(int(v))
    return out


def staleness(
    store: Store,
    snapshot_path: Path,
    cfg: StalenessConfig,
    now: datetime | None = None,
    apply: bool = True,
) -> StalenessReport:
    """Detect stale-candidate pairs. Returns a report of qa_ids by reason.

    With `apply=True` (default), each detected pair has its `stale_reason`
    written to the store unless `user_overridden_stale=1` already.
    """
    now = now or datetime.now(timezone.utc)
    snapshot = _load_snapshot(snapshot_path)
    snapshot_year = int(snapshot.get("year", now.year))
    snapshot_dollars = _snapshot_dollar_amounts(snapshot)

    approved = [p for p in store.list_qa_pairs(status="approved", include_stale=True)
                if not p.user_overridden_stale]

    yb: list[str] = []
    sm: list[str] = []
    cn: list[str] = []

    # 1) year-bound expiry
    for p in approved:
        if p.year_bound is not None and p.year_bound < snapshot_year:
            yb.append(p.qa_id)
            if apply:
                store.mark_stale(p.qa_id, "year_bound_expired")

    # 2) snapshot cross-check on dollar amounts
    for p in approved:
        if not p.canonical_answer or not snapshot_dollars:
            continue
        amounts = {int(s.replace(",", "")) for s in _RE_DOLLAR.findall(p.canonical_answer)}
        if not amounts:
            continue
        # If the answer cites a dollar amount that does not match any current-
        # year canonical figure, flag it.
        if amounts.isdisjoint(snapshot_dollars):
            sm.append(p.qa_id)
            if apply:
                store.mark_stale(p.qa_id, "snapshot_mismatch")

    # 3) within-cluster contradiction
    cn = _within_cluster_contradiction(store, cfg, apply=apply)

    return StalenessReport(year_bound_expired=yb, snapshot_mismatch=sm,
                           contradicted_by_newer=cn)


def _within_cluster_contradiction(store: Store, cfg: StalenessConfig, apply: bool) -> list[str]:
    pairs = {p.qa_id: p for p in store.list_qa_pairs(status="approved", include_stale=True)
             if not p.user_overridden_stale}
    embeddings = {qa_id: vec for qa_id, vec, _m in store.list_embeddings()}
    by_cluster: dict[int, list[QAPair]] = defaultdict(list)
    for p in pairs.values():
        if p.cluster_id is not None:
            by_cluster[int(p.cluster_id)].append(p)
    flagged: list[str] = []
    for members in by_cluster.values():
        if len(members) < 2:
            continue
        members.sort(key=lambda p: p.received_at)
        newest = members[-1]
        nv = embeddings.get(newest.qa_id)
        if nv is None:
            continue
        nv = _normalize(nv)
        for older in members[:-1]:
            ov = embeddings.get(older.qa_id)
            if ov is None:
                continue
            cos = _cosine(nv, _normalize(ov))
            if cos < cfg.contradiction_threshold:
                flagged.append(older.qa_id)
                if apply:
                    store.mark_stale(older.qa_id, "contradicted_by_newer")
    return flagged


def _normalize(vec: list[float]) -> list[float]:
    n = math.sqrt(sum(v * v for v in vec)) or 1.0
    return [v / n for v in vec]


def _cosine(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b))


# --- evergreen -------------------------------------------------------------


def evergreen(store: Store, cfg: StalenessConfig, apply: bool = True) -> dict[int, float]:
    """Score evergreen-ness per cluster: ≥3 distinct calendar years AND tight
    centroid similarity → score = min(1.0, 0.3 * year_span). Returns the per-
    cluster score map."""
    pairs = [p for p in store.list_qa_pairs(status="approved", include_stale=True)]
    embeddings = {qa_id: _normalize(vec) for qa_id, vec, _m in store.list_embeddings()}
    by_cluster: dict[int, list[QAPair]] = defaultdict(list)
    for p in pairs:
        if p.cluster_id is not None:
            by_cluster[int(p.cluster_id)].append(p)
    out: dict[int, float] = {}
    for cid, members in by_cluster.items():
        years = {int(p.received_at[:4]) for p in members
                 if p.received_at and len(p.received_at) >= 4}
        if len(years) < 3:
            continue
        vecs = [embeddings.get(p.qa_id) for p in members]
        vecs = [v for v in vecs if v is not None]
        if len(vecs) < 2:
            continue
        centroid = _normalize([sum(col) / len(vecs) for col in zip(*vecs)])
        sims = [_cosine(centroid, v) for v in vecs]
        avg_sim = sum(sims) / len(sims)
        if avg_sim < cfg.evergreen_threshold:
            continue
        score = min(1.0, 0.3 * (max(years) - min(years)))
        out[cid] = score
        if apply:
            for p in members:
                store.set_evergreen(p.qa_id, score, cluster_id=cid)
    return out


# --- query sampling --------------------------------------------------------


def sample_queries(store: Store, n: int = 20) -> list[dict[str, Any]]:
    return store.list_recent_queries(limit=n)
