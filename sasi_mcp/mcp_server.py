"""MCP server exposing the approved corpus to Claude.

Tool surface (names match the plan; the package was renamed sasi_faq → sasi_mcp
but the tool names stay `search_sasi_faq`, etc., for stability):

* `search_sasi_faq(question, top_k=5, category=None, include_stale=False)`
* `list_categories()`
* `get_qa_pair(qa_id)`
* `flag_qa_pair(qa_id, reason)`
* `corpus_stats()`

The FastMCP-based stdio transport is the default. We do not import `mcp` at
module load time so unit tests can import this file without the dep.
"""

from __future__ import annotations

from collections import Counter
from typing import Any

from sasi_mcp.config import Config
from sasi_mcp.embed import cosine_topk, encode_query
from sasi_mcp.logger import get_logger
from sasi_mcp.retrieval import ScoredPair, score_pair
from sasi_mcp.store import Store

_log = get_logger("sasi_mcp.mcp_server")


def _serialize(pair: ScoredPair) -> dict[str, Any]:
    qa = pair.qa
    received_year: int | None = None
    if qa.received_at:
        try:
            received_year = int(qa.received_at[:4])
        except ValueError:
            received_year = None
    return {
        "qa_id": qa.qa_id,
        "canonical_question": qa.canonical_question,
        "canonical_answer": qa.canonical_answer,
        "category": qa.category,
        "received_year": received_year,
        "evergreen_score": qa.evergreen_score,
        "stale_reason": qa.stale_reason,
        "score": round(pair.final_score, 4),
        "cosine": round(pair.cosine, 4),
        "recency": round(pair.recency, 4),
    }


def search_sasi_faq(
    config: Config,
    store: Store,
    question: str,
    top_k: int = 5,
    category: str | None = None,
    include_stale: bool = False,
) -> list[dict[str, Any]]:
    """Embed the query, do brute-force cosine, then apply retrieval scoring."""
    query_vec = encode_query(config.embedding.model, question)
    candidates = store.list_embeddings()
    if not candidates:
        return []
    # Take more cosine candidates than top_k since scoring may reorder them.
    pool = max(top_k * 4, 20)
    cosine_matches = cosine_topk(query_vec, candidates, pool)

    qa_index = {p.qa_id: p for p in store.list_qa_pairs(include_stale=True)}
    scored: list[ScoredPair] = []
    for m in cosine_matches:
        qa = qa_index.get(m.qa_id)
        if qa is None:
            continue
        if qa.review_status != "approved":
            continue
        if category and qa.category != category:
            continue
        s = score_pair(qa, m.score, config.retrieval, include_stale=include_stale)
        if s.final_score == 0.0 and not include_stale:
            continue
        scored.append(s)
    scored.sort(key=lambda s: s.final_score, reverse=True)
    top = scored[:top_k]
    qa_ids = [s.qa.qa_id for s in top]
    top_score = top[0].final_score if top else None
    store.log_query(question, qa_ids, top_score)
    return [_serialize(s) for s in top]


def list_categories(store: Store) -> dict[str, int]:
    rows = [
        p for p in store.list_qa_pairs(status="approved", include_stale=False)
    ]
    return dict(Counter(p.category or "uncategorized" for p in rows))


def get_qa_pair(store: Store, qa_id: str) -> dict[str, Any] | None:
    qa = store.get_qa_pair(qa_id)
    if qa is None:
        return None
    return {
        "qa_id": qa.qa_id,
        "canonical_question": qa.canonical_question,
        "canonical_answer": qa.canonical_answer,
        "category": qa.category,
        "received_at": qa.received_at,
        "review_status": qa.review_status,
        "stale_reason": qa.stale_reason,
        "evergreen_score": qa.evergreen_score,
        "year_bound": qa.year_bound,
        "temporal_anchors": qa.temporal_anchors,
    }


def flag_qa_pair(store: Store, qa_id: str, reason: str) -> dict[str, Any]:
    """Flip the pair back to pending and record the reason. Used by Claude
    in-conversation when a result is wrong."""
    qa = store.get_qa_pair(qa_id)
    if qa is None:
        return {"ok": False, "error": f"unknown qa_id: {qa_id}"}
    store.update_qa_review(qa_id, status="pending", reviewer="mcp_flag")
    return {"ok": True, "qa_id": qa_id, "reason": reason}


def corpus_stats(store: Store) -> dict[str, Any]:
    return store.stats()


# --- FastMCP wiring (lazy import) ------------------------------------------


def serve(config: Config) -> None:
    """Run the MCP server over stdio. Only called from the CLI."""
    try:
        from mcp.server.fastmcp import FastMCP  # type: ignore[import-not-found]
    except ImportError as exc:
        raise RuntimeError(
            "mcp package not installed. Install with: pip install -e '.[full]'"
        ) from exc

    # The inner @server.tool() functions are intentionally named to match the
    # MCP-published tool names, which shadows this module's same-named impls
    # in the function scope. We resolve through the module to call the impls.
    import sasi_mcp.mcp_server as impl_mod

    server = FastMCP(config.mcp.name)
    store = Store(config.db_path)

    @server.tool()
    def search_sasi_faq(  # noqa: F811
        question: str,
        top_k: int = 5,
        category: str | None = None,
        include_stale: bool = False,
    ) -> list[dict[str, Any]]:
        return impl_mod.search_sasi_faq(config, store, question, top_k, category, include_stale)

    @server.tool()
    def list_categories() -> dict[str, int]:  # noqa: F811
        return impl_mod.list_categories(store)

    @server.tool()
    def get_qa_pair(qa_id: str) -> dict[str, Any] | None:  # noqa: F811
        return impl_mod.get_qa_pair(store, qa_id)

    @server.tool()
    def flag_qa_pair(qa_id: str, reason: str) -> dict[str, Any]:  # noqa: F811
        return impl_mod.flag_qa_pair(store, qa_id, reason)

    @server.tool()
    def corpus_stats() -> dict[str, Any]:  # noqa: F811
        return impl_mod.corpus_stats(store)

    _log.info("mcp_server.start", name=config.mcp.name)
    server.run()
