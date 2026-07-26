"""OpenAI-compatible embeddings client with bounded retries."""

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass
from typing import cast

import httpx
import structlog

from mybot.adapters.payload import as_string_mapping, get_int

logger = structlog.get_logger("mybot.embeddings")

_RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})


class EmbeddingError(RuntimeError):
    """An embeddings request failed; retryable marks transport-level trouble."""

    def __init__(self, message: str, *, retryable: bool) -> None:
        super().__init__(message)
        self.retryable = retryable


@dataclass(slots=True)
class EmbeddingClient:
    """Speaks POST {base_url}/embeddings for any OpenAI-compatible server."""

    client: httpx.AsyncClient
    base_url: str
    api_key: str | None
    model: str
    timeout_seconds: float = 30.0
    max_retries: int = 2
    backoff_base_seconds: float = 0.5

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        if not texts:
            return []
        body = {"model": self.model, "input": list(texts)}
        headers: dict[str, str] = {}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        last_error = EmbeddingError("embeddings were never attempted", retryable=True)
        for attempt in range(self.max_retries + 1):
            if attempt > 0 and self.backoff_base_seconds > 0:
                await asyncio.sleep(
                    min(self.backoff_base_seconds * (2 ** (attempt - 1)), 8.0)
                )
            try:
                response = await self.client.post(
                    f"{self.base_url.rstrip('/')}/embeddings",
                    json=body,
                    headers=headers,
                    timeout=self.timeout_seconds,
                )
            except asyncio.CancelledError:
                raise
            except httpx.HTTPError as error:
                last_error = EmbeddingError(
                    f"embeddings transport failure: {type(error).__name__}",
                    retryable=True,
                )
                logger.warning("embeddings_transport_failure", attempt=attempt)
                continue
            if response.status_code in _RETRYABLE_STATUS:
                last_error = EmbeddingError(
                    f"embeddings returned HTTP {response.status_code}", retryable=True
                )
                continue
            if response.status_code >= 400:
                raise EmbeddingError(
                    f"embeddings rejected with HTTP {response.status_code}",
                    retryable=False,
                )
            return self._parse(response, expected=len(texts))
        raise last_error

    def _parse(self, response: httpx.Response, *, expected: int) -> list[list[float]]:
        payload = as_string_mapping(response.json()) or {}
        raw_data = payload.get("data")
        if not isinstance(raw_data, list):
            raise EmbeddingError("embeddings response had no data list", retryable=False)
        indexed: dict[int, list[float]] = {}
        for raw_entry in cast(list[object], raw_data):
            entry = as_string_mapping(raw_entry)
            if entry is None:
                continue
            index = get_int(entry, "index")
            vector = entry.get("embedding")
            if index is None or not isinstance(vector, list):
                continue
            indexed[index] = [float(cast(float, value)) for value in cast(list[object], vector)]
        if len(indexed) != expected:
            raise EmbeddingError(
                f"embeddings response had {len(indexed)} vectors for {expected} inputs",
                retryable=False,
            )
        return [indexed[position] for position in range(expected)]
