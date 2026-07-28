"""External plugin-runner registration state for API restart recovery."""

import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Protocol, cast

from pydantic import JsonValue
from redis.asyncio import Redis


@dataclass(slots=True, frozen=True)
class PluginRegistration:
    runner_id: str
    protocol_version: int
    manifests: list[JsonValue]


class PluginRegistrationStore(Protocol):
    async def save(self, registration: PluginRegistration) -> None: ...

    async def remove(self, runner_id: str) -> None: ...

    async def touch(self, runner_id: str) -> None: ...

    async def load_all(self) -> tuple[PluginRegistration, ...]: ...

    async def aclose(self) -> None: ...


@dataclass(slots=True)
class MemoryPluginRegistrationStore:
    registrations: dict[str, PluginRegistration] = field(
        default_factory=dict[str, PluginRegistration]
    )

    async def save(self, registration: PluginRegistration) -> None:
        self.registrations[registration.runner_id] = registration

    async def remove(self, runner_id: str) -> None:
        self.registrations.pop(runner_id, None)

    async def touch(self, runner_id: str) -> None:
        return None

    async def load_all(self) -> tuple[PluginRegistration, ...]:
        return tuple(self.registrations[key] for key in sorted(self.registrations))

    async def aclose(self) -> None:
        return None


class RedisPluginRegistrationStore:
    def __init__(
        self,
        client: Redis,
        *,
        ttl_seconds: int = 90,
        prefix: str = "mybot:plugin-registration",
    ) -> None:
        self._client = client
        self._ttl_seconds = ttl_seconds
        self._prefix = prefix

    async def save(self, registration: PluginRegistration) -> None:
        await self._client.set(
            self._key(registration.runner_id),
            json.dumps(
                {
                    "runner_id": registration.runner_id,
                    "protocol_version": registration.protocol_version,
                    "manifests": registration.manifests,
                },
                ensure_ascii=False,
            ),
            ex=self._ttl_seconds,
        )

    async def remove(self, runner_id: str) -> None:
        await self._client.delete(self._key(runner_id))

    async def touch(self, runner_id: str) -> None:
        await self._client.expire(self._key(runner_id), self._ttl_seconds)

    async def load_all(self) -> tuple[PluginRegistration, ...]:
        registrations: list[PluginRegistration] = []
        keys_method = cast(
            Callable[[str], Awaitable[list[object]]],
            self._client.keys,  # pyright: ignore[reportUnknownMemberType]
        )
        keys = await keys_method(f"{self._prefix}:*")
        for unknown_key in keys:
            raw_key = str(unknown_key)
            value = await self._client.get(raw_key)
            if not isinstance(value, str):
                continue
            try:
                decoded = json.loads(value)
                if not isinstance(decoded, dict):
                    continue
                document = cast(dict[str, object], decoded)
                runner_id = document.get("runner_id")
                protocol_version = document.get("protocol_version")
                manifests = document.get("manifests")
                if (
                    not isinstance(runner_id, str)
                    or not isinstance(protocol_version, int)
                    or not isinstance(manifests, list)
                ):
                    continue
                registrations.append(
                    PluginRegistration(
                        runner_id=runner_id,
                        protocol_version=protocol_version,
                        manifests=cast(list[JsonValue], manifests),
                    )
                )
            except (TypeError, ValueError):
                continue
        return tuple(sorted(registrations, key=lambda item: item.runner_id))

    async def aclose(self) -> None:
        await self._client.aclose()

    def _key(self, runner_id: str) -> str:
        return f"{self._prefix}:{runner_id}"


def create_redis_plugin_registration_store(
    redis_url: str, *, ttl_seconds: int
) -> RedisPluginRegistrationStore:
    client = Redis.from_url(redis_url, decode_responses=True)  # pyright: ignore[reportUnknownMemberType]
    return RedisPluginRegistrationStore(client, ttl_seconds=ttl_seconds)


__all__ = [
    "MemoryPluginRegistrationStore",
    "PluginRegistration",
    "PluginRegistrationStore",
    "RedisPluginRegistrationStore",
    "create_redis_plugin_registration_store",
]
