"""C15 — the Groq client, with the key read from `.env` only.

Secret hygiene is the whole point of this module (SC-14):

* The key comes from `config.require_groq_api_key()`, which reads `os.environ`
  after `src.config` has loaded `.env`. It is never read from a source file, a
  CLI flag, or a constant, and never returned by anything in this module.
* The `groq` import is lazy and the client is built inside `complete()`, so a
  missing key produces a `ConfigError` with a clear message rather than an
  import-time crash or a traceback.
* Nothing here logs the key, the request, or the raw headers. Failures are
  converted to `LLMError` carrying a short, non-secret reason.
* Refusals and PII blocks never reach this module at all — `src.answer` returns
  before the client is constructed, which is what makes SC-4's "no LLM call on
  refusal" structurally true rather than merely observed.
"""

from __future__ import annotations

import hashlib
import re
import threading

from src import config

#: Lazily built on first use, never at import time, so a missing key or a
#: missing `groq` package surfaces as a clear message from `complete()` instead
#: of an import-time crash (SC-14).
_CLIENT = None
_CLIENT_LOCK = threading.Lock()

#: SHA-256 of the key the cached client was built with. A digest rather than the
#: key itself, so rotating the key still rebuilds the client without keeping a
#: second plaintext copy of the secret alive in a module global.
_CLIENT_KEY_DIGEST: str | None = None


class LLMError(RuntimeError):
    """A Groq call failed, timed out, or returned an unusable response."""


_CHUNK_ID_LINE = re.compile(
    r"^\s*chunk[_ ]?id\s*[:=]\s*(?P<id>[A-Za-z0-9][A-Za-z0-9-]*)\s*$",
    re.I | re.M,
)
_BARE_ID = re.compile(r"^\s*(?P<id>[a-z0-9]+(?:-[a-z0-9]+)*-\d{3})\s*$", re.I | re.M)


def is_available() -> bool:
    """True when a key is present and a live call could be attempted."""
    return config.has_groq_api_key()


def _key_digest(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def _client():
    """Build and cache the Groq client. Raises if the key is missing.

    `global` is required, not optional: the assignment below makes `_CLIENT` a
    local of this function for its entire body, so without the declaration the
    read in the `if` raises UnboundLocalError on the first call rather than
    reading the module-level cache.
    """
    global _CLIENT, _CLIENT_KEY_DIGEST

    key = config.require_groq_api_key()
    digest = _key_digest(key)

    with _CLIENT_LOCK:
        if _CLIENT is None or _CLIENT_KEY_DIGEST != digest:
            try:
                from groq import Groq
            except ImportError as exc:  # pragma: no cover - dependency is pinned
                raise LLMError(
                    "The 'groq' package is not installed. "
                    "Run: pip install -r requirements.txt"
                ) from exc
            _CLIENT = Groq(api_key=key, timeout=config.GROQ_TIMEOUT)
            _CLIENT_KEY_DIGEST = digest
        return _CLIENT


def complete(messages: list[dict[str, str]], temperature: float | None = None,
             model: str | None = None) -> str:
    """One chat completion. Returns the assistant's text.

    Raises `ConfigError` (via `config.require_groq_api_key`) when no key is
    configured, and `LLMError` for every other failure, so callers never have to
    inspect SDK exception types.
    """
    if temperature is None:
        temperature = config.GROQ_TEMPERATURE
    if model is None:
        model = config.GROQ_MODEL
    try:
        response = _client().chat.completions.create(
            model=model,
            messages=messages,
            temperature=temperature,
            max_tokens=config.GROQ_MAX_TOKENS,
        )
    except config.ConfigError:
        raise
    except Exception as exc:  # noqa: BLE001 - SDK raises a wide family
        raise LLMError(_safe_reason(exc)) from None
    return _text_of(response)


def _safe_reason(exc: Exception) -> str:
    """A short, non-secret description of a failure.

    Only the exception's class and a scrubbed message are used. The API key is
    never part of an SDK error string in the paths we expect, but `scrub` makes
    that guarantee explicit rather than assumed.
    """
    name = type(exc).__name__
    message = str(exc).strip()
    scrub = config.scrub_secrets(message)
    if len(scrub) > 200:
        scrub = scrub[:200] + "..."
    return f"Groq request failed ({name}): {scrub or 'no detail'}"


def _text_of(response) -> str:
    """The assistant's final-channel text, or a clear error.

    A reasoning model that runs out of budget mid-deliberation returns
    `finish_reason="length"` with empty `content`. That used to reach the output
    guard as an empty string and be reported as "answer is empty", which points
    at the prompt and the model rather than at the token budget that actually
    caused it. Distinguishing the two keeps the diagnosis honest, and skipping
    the retry saves a round trip that would fail identically.
    """
    try:
        choice = response.choices[0]
        content = choice.message.content or ""
    except (AttributeError, IndexError, TypeError) as exc:
        raise LLMError("Groq returned a response with no message content") from exc

    if not content.strip():
        finish = getattr(choice, "finish_reason", None)
        if finish == "length":
            raise LLMError(
                "The model spent its whole token budget on reasoning and returned "
                f"no answer (max_tokens={config.GROQ_MAX_TOKENS}); raise "
                "GROQ_MAX_TOKENS"
            )
        raise LLMError("Groq returned an empty message with no truncation flag")
    return content


def extract_chunk_id(text: str) -> str | None:
    """Pull the supporting `chunk_id` out of a model response.

    Prefers the contract line `chunk_id: <id>`. If the model omitted it, a bare
    id on its own line is accepted as a best effort, because refusing to use an
    otherwise-good answer over a formatting slip would be worse than the
    fallback. Anything else returns None and the caller falls back to the
    top-ranked chunk (architecture §4.4).
    """
    match = _CHUNK_ID_LINE.search(text or "")
    if match:
        return match.group("id")
    bare = _BARE_ID.search(text or "")
    if bare:
        return bare.group("id")
    return None


def strip_chunk_id(text: str) -> str:
    """Remove the `chunk_id:` line so it never appears in the rendered answer."""
    without = _CHUNK_ID_LINE.sub("", text or "")
    return "\n".join(line for line in without.splitlines() if line.strip()).strip()


__all__ = [
    "LLMError",
    "complete",
    "extract_chunk_id",
    "is_available",
    "strip_chunk_id",
]
