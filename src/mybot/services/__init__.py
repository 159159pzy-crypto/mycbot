"""Concrete lifecycle services wired per process mode."""

from mybot.runtime import LifecycleService, ProcessMode
from mybot.settings import Settings


def create_service(mode: ProcessMode, settings: Settings) -> LifecycleService | None:
    """Return the real service for a mode, or None to use the idle lifecycle."""

    if mode is ProcessMode.AGENT_WORKER:
        from mybot.services.agent_worker import create_agent_worker_service

        return create_agent_worker_service(settings)
    if mode is ProcessMode.GATEWAY:
        from mybot.services.gateway import create_gateway_service

        return create_gateway_service(settings)
    if mode is ProcessMode.MAINTENANCE_WORKER:
        from mybot.services.maintenance import create_maintenance_service

        return create_maintenance_service(settings)
    if mode is ProcessMode.PLUGIN_RUNNER:
        from mybot.plugins.runner import create_plugin_runner_service

        return create_plugin_runner_service(settings)
    return None
