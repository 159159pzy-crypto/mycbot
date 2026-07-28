"""Per-plugin subprocess supervision with targeted reload and crash circuit breaking."""

import asyncio
import hashlib
import importlib.util
import json
import os
import subprocess
import sys
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from time import monotonic
from typing import Protocol

import httpx
import structlog
from pydantic import JsonValue

from mybot.contracts import PluginManifest
from mybot.plugins.control import PluginControlStore, PluginSource
from mybot.runtime import ProcessMode

logger = structlog.get_logger("mybot.plugins.supervisor")


class ChildProcess(Protocol):
    @property
    def pid(self) -> int: ...

    @property
    def returncode(self) -> int | None: ...

    def terminate(self) -> None: ...

    def kill(self) -> None: ...

    async def wait(self) -> int: ...


class PluginSpawner(Protocol):
    async def spawn(
        self,
        source: PluginSource,
        runner_id: str,
        config: dict[str, JsonValue],
    ) -> ChildProcess: ...


class PluginInspector(Protocol):
    async def inspect(self, source: PluginSource) -> str: ...


@dataclass(slots=True)
class _WindowsChildProcess:
    process: subprocess.Popen[bytes]

    @property
    def pid(self) -> int:
        return self.process.pid

    @property
    def returncode(self) -> int | None:
        return self.process.poll()

    def terminate(self) -> None:
        self.process.terminate()

    def kill(self) -> None:
        self.process.kill()

    async def wait(self) -> int:
        return await asyncio.to_thread(self.process.wait)


@dataclass(slots=True)
class SubprocessPluginInspector:
    timeout_seconds: float = 5.0
    max_output_bytes: int = 64_000

    async def inspect(self, source: PluginSource) -> str:
        environment = os.environ.copy()
        environment.update(
            {
                "MYBOT_PLUGIN_PROBE_ENTRYPOINT": source.entrypoint,
                "MYBOT_PLUGIN_PROBE_PYTHON_PATH": source.python_path or "",
            }
        )
        try:
            completed = await asyncio.to_thread(self._run_probe, environment)
        except subprocess.TimeoutExpired as error:
            raise RuntimeError("plugin manifest probe timed out") from error
        stdout = completed.stdout
        stderr = completed.stderr
        if len(stdout) > self.max_output_bytes or len(stderr) > self.max_output_bytes:
            raise RuntimeError("plugin manifest probe exceeded the output limit")
        if completed.returncode != 0:
            detail = stderr.decode("utf-8", errors="replace").strip()[-500:]
            raise RuntimeError(
                detail or f"plugin manifest probe exited {completed.returncode}"
            )
        for line in reversed(stdout.decode("utf-8", errors="replace").splitlines()):
            try:
                manifest = PluginManifest.model_validate_json(line)
            except ValueError:
                continue
            return manifest.id
        raise RuntimeError("plugin manifest probe returned no valid manifest")

    def _run_probe(
        self, environment: dict[str, str]
    ) -> subprocess.CompletedProcess[bytes]:
        command = (sys.executable, "-m", "mybot.plugins.probe")
        if os.name == "nt":
            return subprocess.run(
                command,
                env=environment,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                timeout=self.timeout_seconds,
                check=False,
                creationflags=0x08000000,  # CREATE_NO_WINDOW
            )
        return subprocess.run(
            command,
            env=environment,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=self.timeout_seconds,
            check=False,
        )


@dataclass(slots=True)
class SubprocessPluginSpawner:
    broker_url: str

    async def spawn(
        self,
        source: PluginSource,
        runner_id: str,
        config: dict[str, JsonValue],
    ) -> ChildProcess:
        environment = os.environ.copy()
        environment.update(
            {
                "MYBOT_PLUGIN_CHILD_BROKER_URL": self.broker_url,
                "MYBOT_PLUGIN_CHILD_ENTRYPOINT": source.entrypoint,
                "MYBOT_PLUGIN_CHILD_RUNNER_ID": runner_id,
                "MYBOT_PLUGIN_CHILD_CONFIG": json.dumps(config, ensure_ascii=False),
                "MYBOT_PLUGIN_CHILD_PYTHON_PATH": source.python_path or "",
            }
        )
        if os.name == "nt":
            process = await asyncio.to_thread(self._spawn_windows, environment)
            return _WindowsChildProcess(process)
        return await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "mybot.plugins.child",
            env=environment,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )

    @staticmethod
    def _spawn_windows(environment: dict[str, str]) -> subprocess.Popen[bytes]:
        return subprocess.Popen(
            (sys.executable, "-m", "mybot.plugins.child"),
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=0x08000000,  # CREATE_NO_WINDOW
        )


@dataclass(slots=True)
class _State:
    source: PluginSource
    runner_id: str
    generation: int
    source_stamp: int | None
    process: ChildProcess | None = None
    crashes: deque[float] = field(default_factory=lambda: deque[float]())
    next_retry_at: float = 0.0
    circuit_open: bool = False
    last_error: str | None = None


@dataclass(slots=True)
class PluginSupervisorService:
    broker_url: str
    config_json: str | None
    store: PluginControlStore
    client: httpx.AsyncClient
    inspector: PluginInspector
    spawner: PluginSpawner
    poll_seconds: float = 1.0
    crash_limit: int = 5
    crash_window_seconds: float = 60.0
    stop_timeout_seconds: float = 5.0
    kill_timeout_seconds: float = 2.0
    clock: Callable[[], float] = monotonic
    _states: dict[str, _State] = field(
        default_factory=lambda: dict[str, _State](), init=False
    )
    _inspections: dict[PluginSource, tuple[int | None, str]] = field(
        default_factory=lambda: dict[PluginSource, tuple[int | None, str]](),
        init=False,
    )

    async def run(self, mode: ProcessMode, stop_event: asyncio.Event) -> None:
        logger.info("plugin_supervisor_started", mode=mode.value)
        try:
            while not stop_event.is_set():
                await self.reconcile()
                try:
                    async with asyncio.timeout(self.poll_seconds):
                        await stop_event.wait()
                except TimeoutError:
                    continue
        finally:
            await asyncio.gather(
                *(self._stop(state) for state in tuple(self._states.values())),
                return_exceptions=True,
            )
            logger.info("plugin_supervisor_stopped", mode=mode.value)

    async def reconcile(self) -> None:
        now = self.clock()
        discovered: dict[str, PluginSource] = {}
        load_errors: dict[str, str] = {}
        for source in self.store.sources(self.config_json):
            stamp = _source_stamp(source)
            try:
                cached = self._inspections.get(source)
                if cached is not None and cached[0] == stamp:
                    plugin_id = cached[1]
                else:
                    plugin_id = await self.inspector.inspect(source)
                    self._inspections[source] = (stamp, plugin_id)
            except Exception as error:
                load_errors[source.entrypoint] = f"{type(error).__name__}: {error}"
                continue
            discovered[plugin_id] = source

        active_sources = set(discovered.values())
        self._inspections = {
            source: inspection
            for source, inspection in self._inspections.items()
            if source in active_sources
        }

        for plugin_id in set(self._states) - set(discovered):
            await self._stop(self._states.pop(plugin_id))

        for plugin_id, source in discovered.items():
            control = self.store.control(plugin_id)
            stamp = _source_stamp(source)
            state = self._states.get(plugin_id)
            if state is None:
                state = _State(
                    source=source,
                    runner_id=_runner_id(plugin_id),
                    generation=control.generation,
                    source_stamp=stamp,
                )
                self._states[plugin_id] = state
            explicit_reload = state.generation != control.generation
            source_changed = state.source != source or state.source_stamp != stamp
            if explicit_reload or source_changed:
                await self._stop(state)
                state.source = source
                state.generation = control.generation
                state.source_stamp = stamp
                state.crashes.clear()
                state.next_retry_at = 0.0
                state.circuit_open = False
                state.last_error = None
            if not control.enabled:
                await self._stop(state)
                continue
            if state.process is not None and state.process.returncode is not None:
                returncode = state.process.returncode
                await self._unregister(state.runner_id)
                state.process = None
                state.last_error = f"process exited with code {returncode}"
                state.crashes.append(now)
                while state.crashes and now - state.crashes[0] > self.crash_window_seconds:
                    state.crashes.popleft()
                if len(state.crashes) >= self.crash_limit:
                    state.circuit_open = True
                else:
                    state.next_retry_at = now + min(2 ** (len(state.crashes) - 1), 30)
            if state.process is None and not state.circuit_open and now >= state.next_retry_at:
                try:
                    state.process = await self.spawner.spawn(
                        source,
                        state.runner_id,
                        dict(control.config or {}),
                    )
                    state.last_error = None
                except Exception as error:
                    state.last_error = f"{type(error).__name__}: {error}"
                    state.next_retry_at = now + 1.0
        self._write_status(discovered, load_errors, now)

    def processes(self) -> tuple[tuple[str, int], ...]:
        return tuple(
            (plugin_id, state.process.pid)
            for plugin_id, state in self._states.items()
            if state.process is not None
        )

    async def _stop(self, state: _State) -> None:
        process = state.process
        if process is None:
            return
        await self._unregister(state.runner_id)
        if process.returncode is None:
            process.terminate()
        try:
            async with asyncio.timeout(self.stop_timeout_seconds):
                await process.wait()
        except TimeoutError:
            logger.warning("plugin_child_stop_timed_out", runner_id=state.runner_id)
            try:
                process.kill()
            except ProcessLookupError:
                pass
            try:
                async with asyncio.timeout(self.kill_timeout_seconds):
                    await process.wait()
            except TimeoutError:
                logger.error("plugin_child_kill_timed_out", runner_id=state.runner_id)
        state.process = None

    async def _unregister(self, runner_id: str) -> None:
        try:
            await self.client.post(
                f"{self.broker_url.rstrip('/')}/plugin-broker/unregister",
                json={"runner_id": runner_id},
                timeout=3.0,
            )
        except httpx.HTTPError:
            logger.warning("plugin_unregister_failed", runner_id=runner_id)

    def _write_status(
        self,
        discovered: dict[str, PluginSource],
        load_errors: dict[str, str],
        now: float,
    ) -> None:
        statuses: dict[str, dict[str, JsonValue]] = {
            key: {"state": "load_error", "last_error": detail}
            for key, detail in load_errors.items()
        }
        for plugin_id in discovered:
            state = self._states[plugin_id]
            control = self.store.control(plugin_id)
            if not control.enabled:
                status = "disabled"
            elif state.circuit_open:
                status = "circuit_open"
            elif state.process is not None:
                status = "running"
            elif now < state.next_retry_at:
                status = "backoff"
            else:
                status = "stopped"
            statuses[plugin_id] = {
                "state": status,
                "pid": state.process.pid if state.process is not None else None,
                "runner_id": state.runner_id,
                "restart_count": len(state.crashes),
                "last_error": state.last_error,
                "generation": control.generation,
                "enabled": control.enabled,
                "next_retry_at": state.next_retry_at or None,
            }
        self.store.write_status(statuses)


def _runner_id(plugin_id: str) -> str:
    digest = hashlib.sha256(plugin_id.encode("utf-8")).hexdigest()[:16]
    return f"plugin-{digest}"


def _source_stamp(source: PluginSource) -> int | None:
    module_name, _, _attribute = source.entrypoint.partition(":")
    if source.python_path:
        root = Path(source.python_path).resolve()
        module = root.joinpath(*module_name.split("."))
        candidates = (module.with_suffix(".py"), module / "__init__.py")
        for candidate in candidates:
            try:
                resolved = candidate.resolve()
                resolved.relative_to(root)
                if resolved.is_file():
                    return resolved.stat().st_mtime_ns
            except (OSError, ValueError):
                continue
        return None
    spec = importlib.util.find_spec(module_name)
    if spec is None or spec.origin is None:
        return None
    try:
        return Path(spec.origin).stat().st_mtime_ns
    except OSError:
        return None


__all__ = [
    "PluginSupervisorService",
    "SubprocessPluginInspector",
    "SubprocessPluginSpawner",
]
