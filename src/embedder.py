"""C6 — the one shared embedder for documents and questions.

architecture §10.1: a single module owns the `SentenceTransformer` instance, and
both pipelines call the same `embed()` function. There is deliberately no second
place in this codebase where a model is constructed — loading the model in two
places, or twice, is a review-checkable violation of the same-model guarantee
that produces the classic "the bot ignores my corpus" failure.

The settings that are easy to forget and silently break comparability are all
pinned in `src.config` and applied on both paths:

| Setting            | Pinned by            |
|--------------------|----------------------|
| model id           | EMBEDDING_MODEL_ID   |
| dimension          | EMBEDDING_DIMENSION  |
| normalisation      | EMBEDDING_NORMALIZE  |
| prompt prefix      | EMBEDDING_PROMPT_PREFIX (empty: MiniLM is not an instruction model) |
| device             | EMBEDDING_DEVICE     |
| max sequence len   | left at the model default, on both paths |

Offline: the model is resolved from the local Hugging Face cache. This module
does not need a Groq key and never sends text anywhere.
"""

from __future__ import annotations

import threading

from src import config
from src.config import ConfigError

#: Process-wide singleton. The model is built once and reused, so ingesting 144
#: chunks and then embedding one question never pays the load cost twice.
_MODEL = None
_LOCK = threading.Lock()


def _model_dimension(model) -> int:
    """Read the model's output width.

    sentence-transformers renamed the accessor; support both so the pinned
    version and future upgrades both work.
    """
    for name in ("get_embedding_dimension", "get_sentence_embedding_dimension"):
        getter = getattr(model, name, None)
        if callable(getter):
            return int(getter())
    raise ConfigError(
        f"Cannot read the embedding dimension from {type(model).__name__}: "
        f"no known dimension accessor was found."
    )


def _build_model():
    """Construct the model and assert it matches the pinned configuration."""
    # Imported here, not at module scope, so that importing `src.config` or any
    # other module never pulls in torch.
    import torch
    from sentence_transformers import SentenceTransformer

    # Determinism: the encoder itself is deterministic, but seeding means two
    # runs cannot diverge through any library-level randomness.
    torch.manual_seed(config.EMBEDDING_SEED)

    model = SentenceTransformer(
        config.EMBEDDING_MODEL_ID,
        device=config.EMBEDDING_DEVICE,
    )

    actual = _model_dimension(model)
    if actual != config.EMBEDDING_DIMENSION:
        # Abort before anything is written to the store. A wrong dimension means
        # the wrong model, and storing those vectors produces a store that fails
        # silently at query time.
        raise ConfigError(
            f"Embedding dimension mismatch: config expects "
            f"{config.EMBEDDING_DIMENSION} but {config.EMBEDDING_MODEL_ID!r} "
            f"produces {actual}. Ingestion aborted; no store was written. "
            f"Fix EMBEDDING_DIMENSION in src/config.py to match the model, or "
            f"change EMBEDDING_MODEL_ID to a {config.EMBEDDING_DIMENSION}"
            f"-dimension model, then re-run ingestion."
        )
    return model


def get_model():
    """Return the shared model, building it on first use."""
    global _MODEL
    if _MODEL is None:
        with _LOCK:
            if _MODEL is None:
                _MODEL = _build_model()
    return _MODEL


def is_loaded() -> bool:
    """True if the model has already been built in this process."""
    return _MODEL is not None


def embed(texts: list[str], show_progress: bool = False) -> list[list[float]]:
    """Embed text with the shared model. The only embedding entry point.

    Ingestion passes all 144 chunks in one call; the query path will pass a
    single-element list for a user question. Both go through this function, so
    they cannot diverge.
    """
    if not texts:
        return []

    model = get_model()
    vectors = model.encode(
        texts,
        batch_size=config.EMBEDDING_BATCH_SIZE,
        convert_to_numpy=True,
        normalize_embeddings=config.EMBEDDING_NORMALIZE,
        show_progress_bar=show_progress,
    )

    # Assert the shape of what we are about to store, not just of the model.
    # Cheap, and it is the last chance to catch a bad vector before it is
    # written to the store.
    for i, vector in enumerate(vectors):
        if len(vector) != config.EMBEDDING_DIMENSION:
            raise ConfigError(
                f"Embedder returned {len(vector)} dimensions for input "
                f"{i}, expected {config.EMBEDDING_DIMENSION}. Aborting; no "
                f"store was written."
            )

    return [vector.tolist() for vector in vectors]


def embed_one(text: str) -> list[float]:
    """Embed a single string. A thin wrapper over `embed()`, not a second path."""
    return embed([text])[0]


__all__ = [
    "embed",
    "embed_one",
    "get_model",
    "is_loaded",
]
