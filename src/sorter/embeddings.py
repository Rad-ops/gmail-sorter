"""Embedding-based semantic classification for the Gmail sorter.

The keyword rules in :mod:`sorter.policy` are fast and explainable but
fundamentally lexical — they cannot understand context, intent, or the
relationship between words. This module adds an optional semantic layer:
each message's subject + body excerpt is embedded into a dense vector and
compared to per-category centroid vectors learned from past high-confidence
decisions.

The hybrid scoring in ``decide()`` takes ``max(keyword_confidence,
embedding_similarity * 100)`` per category, so the keyword rules provide the
explainable floor and the embedding provides the semantic ceiling. When no
embedding backend is available, the sorter falls back to keyword-only scoring
(current behavior).

Two backends are supported:
  1. HTTP endpoint (local LLM server's ``/v1/embeddings``) — lightweight, no
     Python dependencies, but requires the server to be running.
  2. sentence-transformers (if installed) — fully offline, but pulls in
     PyTorch.

All vector math is pure Python (no numpy) so the sorter stays lightweight.
Embeddings are not reversible — they do not contain readable email content.
"""

from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request
from typing import Any, Protocol

log = logging.getLogger("sorter.embeddings")


class EmbeddingBackend(Protocol):
    """A callable that produces a dense vector from text."""

    def embed(self, text: str) -> list[float] | None:
        ...


def cosine_similarity(a: list[float], b: list[float]) -> float:
    """Pure-Python cosine similarity. Returns 0.0 for empty/zero vectors."""

    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = sum(x * x for x in a) ** 0.5
    norm_b = sum(y * y for y in b) ** 0.5
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return max(0.0, min(1.0, dot / (norm_a * norm_b)))


def average_vectors(vectors: list[list[float]]) -> list[float]:
    """Element-wise average of a list of equal-length vectors."""

    if not vectors:
        return []
    dim = len(vectors[0])
    result = [0.0] * dim
    for vec in vectors:
        for i, val in enumerate(vec):
            if i < dim:
                result[i] += val
    return [v / len(vectors) for v in result]


class HttpEmbeddingBackend:
    """Embedding backend that calls a local OpenAI-compatible /v1/embeddings."""

    def __init__(self, endpoint: str, model: str = "local", timeout: float = 5.0) -> None:
        self.endpoint = endpoint.rstrip("/")
        self.model = model
        self.timeout = timeout

    def embed(self, text: str) -> list[float] | None:
        if not text:
            return None
        payload = json.dumps({"model": self.model, "input": text[:4000]}).encode("utf-8")
        req = urllib.request.Request(
            self.endpoint,
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                return data.get("data", [{}])[0].get("embedding")
        except Exception as error:
            log.debug("HTTP embedding failed: %s", error)
            return None


class SentenceTransformerBackend:
    """Embedding backend using sentence-transformers (fully offline)."""

    _model: Any = None
    _model_name: str

    def __init__(self, model_name: str = "all-MiniLM-L6-v2") -> None:
        self._model_name = model_name

    def _ensure_model(self) -> Any:
        if self._model is None:
            from sentence_transformers import SentenceTransformer  # type: ignore
            self.__class__._model = SentenceTransformer(self._model_name)
        return self._model

    def embed(self, text: str) -> list[float] | None:
        if not text:
            return None
        try:
            model = self._ensure_model()
            vec = model.encode(text[:4000])
            return [float(x) for x in vec.tolist()]
        except Exception as error:
            log.debug("sentence-transformers embedding failed: %s", error)
            return None


def create_embedding_backend(endpoint: str = "", model: str = "local", st_model: str = "") -> EmbeddingBackend | None:
    """Create the best available embedding backend.

    Tries the HTTP endpoint first (lightweight), then sentence-transformers
    (offline but heavier). Returns None when neither is available so callers
    can fall back to keyword-only scoring.
    """

    if endpoint:
        log.info("using HTTP embedding backend at %s", endpoint)
        return HttpEmbeddingBackend(endpoint, model=model)
    if st_model:
        try:
            import sentence_transformers  # type: ignore  # noqa: F401
            log.info("using sentence-transformers backend (%s)", st_model)
            return SentenceTransformerBackend(st_model)
        except ImportError:
            log.warning("sentence-transformers requested but not installed; falling back to keyword-only")
    return None


def compute_embedding_scores(
    text: str,
    centroids: dict[str, list[float]],
    backend: EmbeddingBackend | None,
) -> dict[str, float]:
    """Return {category: similarity 0-1} for the text against each centroid.

    Returns an empty dict when the backend is unavailable or the text is empty,
    so callers can fall back to keyword-only scoring without special handling.
    """

    if not backend or not text or not centroids:
        return {}
    vec = backend.embed(text)
    if not vec:
        return {}
    return {cat: cosine_similarity(vec, centroid) for cat, centroid in centroids.items() if len(centroid) == len(vec)}
