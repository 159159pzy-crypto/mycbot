"""Shared, atomic control files for plugin supervision and trusted installs."""

import json
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from pydantic import JsonValue

from mybot.contracts.json import parse_json


@dataclass(slots=True, frozen=True)
class PluginSource:
    entrypoint: str
    python_path: str | None = None


@dataclass(slots=True, frozen=True)
class PluginControl:
    enabled: bool = True
    generation: int = 0
    config: dict[str, JsonValue] | None = None


class PluginControlStore:
    def __init__(self, root: Path | str) -> None:
        self.root = Path(root).resolve()

    def sources(self, config_json: str | None) -> tuple[PluginSource, ...]:
        sources: list[PluginSource] = []
        if config_json:
            try:
                decoded = parse_json(config_json)
            except ValueError:
                decoded = {}
            entries = decoded.get("plugins") if isinstance(decoded, dict) else None
            if isinstance(entries, list):
                for raw in entries:
                    source = _source(raw)
                    if source is not None:
                        sources.append(source)
        managed = self._read("managed.json")
        entries = managed.get("plugins")
        if isinstance(entries, list):
            for raw in entries:
                source = _source(raw)
                if source is not None:
                    sources.append(source)
        unique: dict[tuple[str, str | None], PluginSource] = {}
        for source in sources:
            unique[(source.entrypoint, source.python_path)] = source
        return tuple(unique.values())

    def control(self, plugin_id: str) -> PluginControl:
        document = self._read("control.json")
        plugins = document.get("plugins")
        raw = plugins.get(plugin_id) if isinstance(plugins, dict) else None
        if not isinstance(raw, dict):
            return PluginControl(config={})
        config = raw.get("config")
        generation = raw.get("generation", 0)
        return PluginControl(
            enabled=raw.get("enabled", True) is not False,
            generation=max(0, generation)
            if isinstance(generation, int) and not isinstance(generation, bool)
            else 0,
            config=(
                config if isinstance(config, dict) else {}
            ),
        )

    def request_action(self, plugin_id: str, action: str) -> PluginControl:
        document = self._read("control.json")
        plugins = document.setdefault("plugins", {})
        if not isinstance(plugins, dict):
            plugins = {}
            document["plugins"] = plugins
        current = plugins.get(plugin_id)
        raw = dict(current) if isinstance(current, dict) else {}
        if action == "enable":
            raw["enabled"] = True
        elif action == "disable":
            raw["enabled"] = False
        elif action == "reload":
            raw["enabled"] = True
            generation = raw.get("generation", 0)
            raw["generation"] = (generation if isinstance(generation, int) else 0) + 1
        else:
            raise ValueError("action must be enable, disable, or reload")
        plugins[plugin_id] = raw
        self._write("control.json", document)
        return self.control(plugin_id)

    def set_config(self, plugin_id: str, config: dict[str, JsonValue]) -> PluginControl:
        document = self._read("control.json")
        plugins = document.setdefault("plugins", {})
        if not isinstance(plugins, dict):
            plugins = {}
            document["plugins"] = plugins
        current = plugins.get(plugin_id)
        raw = dict(current) if isinstance(current, dict) else {}
        raw["config"] = config
        plugins[plugin_id] = raw
        self._write("control.json", document)
        return self.control(plugin_id)

    def status(self) -> dict[str, JsonValue]:
        return self._read("status.json")

    def write_status(self, plugins: dict[str, dict[str, JsonValue]]) -> None:
        self._write("status.json", {"plugins": cast(JsonValue, plugins)})

    def managed(self) -> dict[str, JsonValue]:
        return self._read("managed.json")

    def add_managed(self, entry: dict[str, JsonValue]) -> None:
        document = self._read("managed.json")
        entries = document.get("plugins")
        plugins: list[JsonValue] = list(entries) if isinstance(entries, list) else []
        plugin_id = entry.get("id")
        plugins = [
            item
            for item in plugins
            if not isinstance(item, dict) or item.get("id") != plugin_id
        ]
        plugins.append(entry)
        self._write("managed.json", {"plugins": plugins})

    def _read(self, name: str) -> dict[str, JsonValue]:
        try:
            decoded = parse_json((self.root / name).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return decoded if isinstance(decoded, dict) else {}

    def _write(self, name: str, value: dict[str, JsonValue]) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        destination = self.root / name
        temporary = self.root / f".{name}.tmp"
        temporary.write_text(
            json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
            newline="\n",
        )
        temporary.replace(destination)


def _source(raw: JsonValue) -> PluginSource | None:
    if isinstance(raw, str) and ":" in raw:
        return PluginSource(entrypoint=raw)
    if not isinstance(raw, dict):
        return None
    entrypoint = raw.get("entrypoint")
    python_path = raw.get("python_path")
    if not isinstance(entrypoint, str) or ":" not in entrypoint:
        return None
    return PluginSource(
        entrypoint=entrypoint,
        python_path=python_path if isinstance(python_path, str) else None,
    )


__all__ = ["PluginControl", "PluginControlStore", "PluginSource"]
