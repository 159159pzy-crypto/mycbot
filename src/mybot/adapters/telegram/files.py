"""Secret-safe, bounded Telegram image resolution for vision requests."""

import base64
import mimetypes
from dataclasses import dataclass, field
from urllib.parse import quote

import httpx

from mybot.adapters.payload import as_string_mapping, get_mapping, get_str
from mybot.contracts import ImageSegment

_SUPPORTED_IMAGE_TYPES = frozenset(
    {"image/gif", "image/jpeg", "image/png", "image/webp"}
)


class TelegramMediaError(RuntimeError):
    """A stable error code that never includes the bot token or Telegram URL."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(slots=True)
class TelegramImageResolver:
    token: str = field(repr=False)
    client: httpx.AsyncClient
    api_base_url: str = "https://api.telegram.org"
    max_bytes: int = 10_000_000
    timeout_seconds: float = 30.0

    async def resolve(self, segment: ImageSegment) -> ImageSegment:
        """Resolve only internal Telegram references; public/data URLs pass through."""

        if not segment.url.startswith("tg-file://"):
            return segment
        file_id = segment.url.removeprefix("tg-file://").strip()
        if not file_id:
            raise TelegramMediaError("invalid_file_id")
        file_path = await self._file_path(file_id)
        content, media_type = await self._download(file_path)
        encoded = base64.b64encode(content).decode("ascii")
        return segment.model_copy(update={"url": f"data:{media_type};base64,{encoded}"})

    async def _file_path(self, file_id: str) -> str:
        try:
            response = await self.client.get(
                f"{self.api_base_url.rstrip('/')}/bot{self.token}/getFile",
                params={"file_id": file_id},
                timeout=self.timeout_seconds,
            )
        except httpx.HTTPError:
            raise TelegramMediaError("get_file_failed") from None
        if response.status_code >= 400:
            raise TelegramMediaError("get_file_failed")
        try:
            payload = as_string_mapping(response.json()) or {}
        except ValueError:
            raise TelegramMediaError("get_file_invalid_response") from None
        result = get_mapping(payload, "result")
        file_path = get_str(result, "file_path") if result is not None else None
        if payload.get("ok") is not True or file_path is None:
            raise TelegramMediaError("get_file_invalid_response")
        normalized = file_path.strip("/")
        if not normalized or any(part in {"", ".", ".."} for part in normalized.split("/")):
            raise TelegramMediaError("invalid_file_path")
        return normalized

    async def _download(self, file_path: str) -> tuple[bytes, str]:
        encoded_path = quote(file_path, safe="/")
        url = f"{self.api_base_url.rstrip('/')}/file/bot{self.token}/{encoded_path}"
        try:
            async with self.client.stream("GET", url, timeout=self.timeout_seconds) as response:
                if response.status_code >= 400:
                    raise TelegramMediaError("image_download_failed")
                declared_size = _content_length(response.headers.get("content-length"))
                if declared_size is not None and declared_size > self.max_bytes:
                    raise TelegramMediaError("image_too_large")
                media_type = _image_media_type(
                    response.headers.get("content-type"), file_path
                )
                chunks: list[bytes] = []
                size = 0
                async for chunk in response.aiter_bytes():
                    size += len(chunk)
                    if size > self.max_bytes:
                        raise TelegramMediaError("image_too_large")
                    chunks.append(chunk)
        except TelegramMediaError:
            raise
        except httpx.HTTPError:
            raise TelegramMediaError("image_download_failed") from None
        if not chunks:
            raise TelegramMediaError("image_empty")
        return b"".join(chunks), media_type


def _content_length(value: str | None) -> int | None:
    if value is None:
        return None
    try:
        return max(0, int(value))
    except ValueError:
        return None


def _image_media_type(content_type: str | None, file_path: str) -> str:
    media_type = (content_type or "").partition(";")[0].strip().lower()
    if media_type not in _SUPPORTED_IMAGE_TYPES:
        guessed, _ = mimetypes.guess_type(file_path)
        media_type = (guessed or "").lower()
    if media_type not in _SUPPORTED_IMAGE_TYPES:
        raise TelegramMediaError("unsupported_image_type")
    return media_type
