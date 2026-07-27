import base64

import httpx
import pytest
from platform_payloads import telegram_private_message, telegram_update

from mybot.adapters.telegram.files import TelegramImageResolver, TelegramMediaError
from mybot.adapters.telegram.translate import translate_telegram_update
from mybot.contracts import ImageSegment
from mybot.engine.vision import VisionMode, VisionService
from mybot.infrastructure.llm import ImageContentPart


@pytest.mark.asyncio
async def test_resolver_downloads_telegram_image_as_secret_free_data_url() -> None:
    seen_paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_paths.append(request.url.path)
        if request.url.path.endswith("/getFile"):
            assert request.url.params["file_id"] == "photo-1"
            return httpx.Response(
                200,
                json={"ok": True, "result": {"file_path": "photos/image_1.jpg"}},
            )
        if request.url.path.endswith("/file/botsecret-token/photos/image_1.jpg"):
            return httpx.Response(
                200,
                content=b"jpeg-bytes",
                headers={"content-type": "image/jpeg"},
            )
        raise AssertionError(f"unexpected Telegram API path: {request.url.path}")

    resolver = TelegramImageResolver(
        token="secret-token",
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        api_base_url="https://api.telegram.example",
    )
    assert "secret-token" not in repr(resolver)

    resolved = await resolver.resolve(ImageSegment(url="tg-file://photo-1", alt_text="照片"))

    encoded = base64.b64encode(b"jpeg-bytes").decode("ascii")
    assert resolved == ImageSegment(
        url=f"data:image/jpeg;base64,{encoded}",
        alt_text="照片",
    )
    assert seen_paths == [
        "/botsecret-token/getFile",
        "/file/botsecret-token/photos/image_1.jpg",
    ]
    await resolver.client.aclose()


@pytest.mark.asyncio
async def test_resolver_rejects_oversized_images_without_exposing_token() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/getFile"):
            return httpx.Response(
                200,
                json={"ok": True, "result": {"file_path": "photos/large.png"}},
            )
        return httpx.Response(
            200,
            content=b"too-large",
            headers={"content-type": "image/png", "content-length": "9"},
        )

    resolver = TelegramImageResolver(
        token="secret-token",
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        api_base_url="https://api.telegram.example",
        max_bytes=8,
    )

    with pytest.raises(TelegramMediaError) as captured:
        await resolver.resolve(ImageSegment(url="tg-file://photo-1"))

    assert captured.value.code == "image_too_large"
    assert "secret-token" not in str(captured.value)
    await resolver.client.aclose()


@pytest.mark.asyncio
async def test_resolver_leaves_public_image_urls_unchanged() -> None:
    resolver = TelegramImageResolver(
        token="secret-token",
        client=httpx.AsyncClient(),
        api_base_url="https://api.telegram.example",
    )
    segment = ImageSegment(url="https://img.example/cat.png")

    assert await resolver.resolve(segment) is segment
    await resolver.client.aclose()


@pytest.mark.asyncio
async def test_telegram_update_reaches_direct_vision_as_data_url() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/getFile"):
            return httpx.Response(
                200,
                json={"ok": True, "result": {"file_path": "photos/direct.webp"}},
            )
        return httpx.Response(
            200,
            content=b"webp-image",
            headers={"content-type": "image/webp"},
        )

    event = translate_telegram_update(
        telegram_update(
            telegram_private_message(
                text=None,
                caption="分析图片",
                photo=[{"file_id": "photo-direct", "width": 640, "height": 480}],
            )
        ),
        connection_id="telegram-main",
        self_id=424242,
        bot_username="mybot_bot",
    )
    assert event is not None
    resolver = TelegramImageResolver(
        token="secret-token",
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        api_base_url="https://api.telegram.example",
    )

    prepared = await VisionService(
        llm=None,
        mode=VisionMode.DIRECT,
        image_resolver=resolver,
    ).prepare(event.envelope)

    assert not isinstance(prepared.content, str)
    image_parts = [part for part in prepared.content if isinstance(part, ImageContentPart)]
    assert len(image_parts) == 1
    assert image_parts[0].image_url.url.startswith("data:image/webp;base64,")
    assert event.envelope.segments[-1] == ImageSegment(url="tg-file://photo-direct")
    await resolver.client.aclose()
