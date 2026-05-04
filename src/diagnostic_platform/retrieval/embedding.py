"""Embedding clients for dense retrieval."""

from __future__ import annotations

import hashlib
import math
import os
from typing import Protocol


class EmbeddingClient(Protocol):
    """Minimal embedding interface used by vector indexing and querying."""

    model_name: str

    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        """Return one embedding vector per text."""


class SentenceTransformerEmbeddingClient:
    """Local sentence-transformers embedding client."""

    def __init__(
        self,
        model_name: str = "BAAI/bge-m3",
        *,
        device: str | None = None,
        normalize: bool = True,
        max_seq_length: int | None = 512,
    ) -> None:
        try:
            from sentence_transformers import SentenceTransformer
        except ImportError as exc:
            raise RuntimeError(
                "sentence-transformers is required for local embeddings. "
                "Install the vector extras or pass a test/fake EmbeddingClient."
            ) from exc

        self.model_name = model_name
        self.normalize = normalize
        self.model = SentenceTransformer(model_name, device=device)
        if max_seq_length is not None and max_seq_length > 0:
            self.model.max_seq_length = max_seq_length

    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        vectors = self.model.encode(
            texts,
            batch_size=min(max(len(texts), 1), 64),
            normalize_embeddings=self.normalize,
            show_progress_bar=False,
        )
        return [list(map(float, vector)) for vector in vectors]


class HashingEmbeddingClient:
    """Deterministic local embedding client used for tests and smoke runs."""

    def __init__(self, dimension: int = 16, model_name: str = "hashing-test-embedding") -> None:
        self.dimension = dimension
        self.model_name = model_name

    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        return [_normalize(_hashing_vector(text, self.dimension)) for text in texts]


def build_embedding_client(model_name: str = "BAAI/bge-m3") -> EmbeddingClient:
    """Build the default local embedding client."""

    if model_name.startswith("hashing:"):
        raw_dimension = model_name.split(":", 1)[1] or "16"
        return HashingEmbeddingClient(dimension=int(raw_dimension), model_name=model_name)
    max_seq_length = int(os.environ.get("EMBEDDING_MAX_SEQ_LENGTH") or "512")
    return SentenceTransformerEmbeddingClient(
        model_name=model_name,
        device=os.environ.get("EMBEDDING_DEVICE") or None,
        max_seq_length=max_seq_length,
    )


def _hashing_vector(text: str, dimension: int) -> list[float]:
    vector = [0.0 for _ in range(dimension)]
    for token in text.lower().split():
        digest = hashlib.sha256(token.encode("utf-8")).digest()
        index = int.from_bytes(digest[:4], "big") % dimension
        sign = 1.0 if digest[4] % 2 == 0 else -1.0
        vector[index] += sign
    if not any(vector):
        digest = hashlib.sha256(text.encode("utf-8")).digest()
        for index in range(dimension):
            vector[index] = (digest[index % len(digest)] - 127.5) / 127.5
    return vector


def _normalize(vector: list[float]) -> list[float]:
    norm = math.sqrt(sum(value * value for value in vector))
    if norm <= 0:
        return vector
    return [value / norm for value in vector]
