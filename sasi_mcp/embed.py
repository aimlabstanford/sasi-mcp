"""Sentence-transformers embeddings + brute-force cosine search.

Only `review_status='approved'` pairs are embedded. The vector signal is
`Q: <canonical_question>\\nA: <first 600 chars of canonical_answer>` so a
question that paraphrases the question text alone or quotes a fragment of
the answer both retrieve correctly.

For a few thousand vectors, brute-force cosine in Python is faster than the
sqlite-vss bring-up cost. We pack everything as a single `numpy.ndarray` if
numpy is available, else fall back to pure-Python list math.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from sasi_mcp.logger import get_logger
from sasi_mcp.store import Store

_log = get_logger("sasi_mcp.embed")

_ANSWER_TRUNC_CHARS = 600


@dataclass(slots=True)
class EmbeddingMatch:
    qa_id: str
    score: float


def _build_signal(canonical_question: str, canonical_answer: str) -> str:
    answer = (canonical_answer or "")[:_ANSWER_TRUNC_CHARS]
    return f"Q: {canonical_question or ''}\nA: {answer}"


def _l2_normalize(vec: list[float]) -> list[float]:
    n = math.sqrt(sum(v * v for v in vec)) or 1.0
    return [v / n for v in vec]


def _load_model(model_name: str) -> Any:
    try:
        from sentence_transformers import SentenceTransformer  # type: ignore[import-not-found]
    except ImportError as exc:
        raise RuntimeError(
            "sentence-transformers not installed. "
            "Install the [full] extra: pip install -e '.[full]'"
        ) from exc
    return SentenceTransformer(model_name)


def embed_approved(store: Store, model_name: str) -> int:
    """Embed every approved pair lacking an embedding. Returns the count of
    new embeddings written."""
    approved = store.list_qa_pairs(status="approved")
    existing_ids = {qa_id for qa_id, _v, _m in store.list_embeddings()}
    todo = [p for p in approved if p.qa_id not in existing_ids and p.canonical_question]
    if not todo:
        return 0
    model = _load_model(model_name)
    signals = [_build_signal(p.canonical_question or "", p.canonical_answer or "")
               for p in todo]
    vectors = model.encode(signals, normalize_embeddings=True, convert_to_numpy=False)
    for pair, vec in zip(todo, vectors):
        store.upsert_embedding(pair.qa_id, [float(x) for x in vec], model=model_name)
    return len(todo)


def encode_query(model_name: str, question: str) -> list[float]:
    model = _load_model(model_name)
    vec = model.encode([question], normalize_embeddings=True, convert_to_numpy=False)[0]
    return [float(x) for x in vec]


def cosine_topk(
    query_vec: list[float],
    candidates: list[tuple[str, list[float], str]],
    top_k: int,
) -> list[EmbeddingMatch]:
    """Brute-force cosine over normalized vectors. If embeddings were stored
    pre-normalization, we re-normalize the query once."""
    qn = _l2_normalize(query_vec)
    scored: list[EmbeddingMatch] = []
    for qa_id, vec, _model in candidates:
        # Defensive: re-normalize stored vectors too in case they weren't.
        cn = _l2_normalize(vec) if abs(sum(v * v for v in vec) - 1.0) > 0.05 else vec
        score = sum(a * b for a, b in zip(qn, cn))
        scored.append(EmbeddingMatch(qa_id=qa_id, score=score))
    scored.sort(key=lambda m: m.score, reverse=True)
    return scored[:top_k]
