import pytest

from mybot.plugins.broker import PluginBroker
from mybot.plugins.state import MemoryPluginRegistrationStore


def manifest_payload() -> dict[str, object]:
    return {
        "id": "example.persisted",
        "version": "1.0.0",
        "entrypoint": "example:plugin",
        "requested_capabilities": [],
        "tools": [],
    }


@pytest.mark.asyncio
async def test_broker_restores_registration_after_restart() -> None:
    store = MemoryPluginRegistrationStore()
    first = PluginBroker(grants={}, registrations=store)
    first.register("runner-1", [manifest_payload()], protocol_version=2)
    await first.persist("runner-1")

    restarted = PluginBroker(grants={}, registrations=store)
    assert await restarted.restore() == 1
    health = restarted.health()

    assert health["runners"] == 1
    assert health["plugins"][0]["id"] == "example.persisted"  # type: ignore[index]


@pytest.mark.asyncio
async def test_unregister_removes_persisted_registration() -> None:
    store = MemoryPluginRegistrationStore()
    broker = PluginBroker(grants={}, registrations=store)
    broker.register("runner-1", [manifest_payload()])
    await broker.persist("runner-1")
    broker.unregister("runner-1")
    await broker.forget("runner-1")

    assert await store.load_all() == ()
