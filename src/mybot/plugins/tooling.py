"""Plugin scaffolding and fail-closed installation from the trusted registry."""

import hashlib
import io
import json
import re
import shutil
import stat
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import cast

import httpx

from mybot.contracts import PluginManifest
from mybot.contracts.json import parse_json
from mybot.plugins.control import PluginControlStore

_SLUG = re.compile(r"^[a-z][a-z0-9_]*$")


@dataclass(slots=True, frozen=True)
class InstalledPlugin:
    manifest: PluginManifest
    install_dir: Path


def scaffold_plugin(name: str, root: Path | str = "plugins") -> Path:
    slug = name.strip().lower().replace("-", "_")
    if _SLUG.fullmatch(slug) is None:
        raise ValueError("plugin name must start with a letter and contain letters, digits, or _")
    destination = Path(root).resolve() / slug
    if destination.exists():
        raise FileExistsError(destination)
    package = destination / "src" / slug
    tests = destination / "tests"
    package.mkdir(parents=True)
    tests.mkdir(parents=True)
    manifest = {
        "id": f"local.{slug}",
        "version": "0.1.0",
        "entrypoint": f"{slug}.plugin:PLUGIN",
        "event_hooks": [],
        "tools": [],
        "tasks": [{"id": "refresh", "interval_seconds": 3600}],
        "config_schema": {
            "type": "object",
            "properties": {"enabled": {"type": "boolean", "default": True}},
            "additionalProperties": False,
        },
        "platforms": [],
        "requested_capabilities": [],
        "requires": {},
    }
    destination.joinpath("plugin.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    destination.joinpath("pyproject.toml").write_text(
        f'[project]\nname = "mybot-plugin-{slug}"\nversion = "0.1.0"\n'
        'requires-python = ">=3.12"\n\n[tool.pytest.ini_options]\npythonpath = ["src"]\n',
        encoding="utf-8",
        newline="\n",
    )
    package.joinpath("__init__.py").write_text("", encoding="utf-8")
    package.joinpath("plugin.py").write_text(
        _plugin_template(slug), encoding="utf-8", newline="\n"
    )
    tests.joinpath("test_plugin.py").write_text(
        _test_template(slug), encoding="utf-8", newline="\n"
    )
    return destination


@dataclass(slots=True)
class PluginInstaller:
    registry_path: Path
    store: PluginControlStore
    client: httpx.AsyncClient
    max_bytes: int = 10_000_000

    def registry(self) -> list[dict[str, str]]:
        try:
            decoded = parse_json(self.registry_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            raise ValueError("plugin registry is unreadable") from error
        raw_plugins = decoded.get("plugins") if isinstance(decoded, dict) else None
        if not isinstance(raw_plugins, list):
            raise ValueError("plugin registry must contain a plugins list")
        entries: list[dict[str, str]] = []
        for raw in raw_plugins:
            if not isinstance(raw, dict):
                continue
            required = ("name", "version", "source_url", "manifest_sha256")
            if all(isinstance(raw.get(key), str) for key in required):
                entries.append({key: cast(str, raw[key]) for key in required})
        return entries

    async def install(self, name: str) -> InstalledPlugin:
        entry = next((item for item in self.registry() if item["name"] == name), None)
        if entry is None:
            raise KeyError(name)
        if not entry["source_url"].startswith("https://"):
            raise ValueError("plugin source_url must use HTTPS")
        response = await self.client.get(entry["source_url"], timeout=30.0)
        response.raise_for_status()
        payload = response.content
        if len(payload) > self.max_bytes:
            raise ValueError("plugin archive exceeds the download size limit")
        try:
            archive = zipfile.ZipFile(io.BytesIO(payload))
        except zipfile.BadZipFile as error:
            raise ValueError("plugin archive is not a valid zip") from error
        with archive:
            infos = archive.infolist()
            for info in infos:
                if _unsafe(info):
                    raise ValueError("plugin archive contains an unsafe path or link")
            try:
                manifest_bytes = archive.read("plugin.json")
            except KeyError as error:
                raise ValueError("plugin archive is missing plugin.json") from error
            digest = hashlib.sha256(manifest_bytes).hexdigest()
            if digest.lower() != entry["manifest_sha256"].lower():
                raise ValueError("plugin manifest hash does not match the trusted registry")
            try:
                manifest = PluginManifest.model_validate_json(manifest_bytes)
            except Exception as error:
                raise ValueError("plugin manifest is invalid") from error
            if manifest.id != entry["name"] or manifest.version != entry["version"]:
                raise ValueError("plugin manifest identity does not match the registry")
            installed_root = self.store.root / "installed" / manifest.id
            destination = installed_root / manifest.version
            if not destination.exists():
                temporary = installed_root / f".{manifest.version}.installing"
                if temporary.exists():
                    shutil.rmtree(temporary)
                temporary.mkdir(parents=True)
                try:
                    for info in infos:
                        if info.is_dir():
                            continue
                        target = temporary / PurePosixPath(info.filename)
                        target.parent.mkdir(parents=True, exist_ok=True)
                        target.write_bytes(archive.read(info))
                    temporary.replace(destination)
                except Exception:
                    shutil.rmtree(temporary, ignore_errors=True)
                    raise
        python_path = destination / "src"
        if not python_path.is_dir():
            python_path = destination
        self.store.add_managed(
            {
                "id": manifest.id,
                "version": manifest.version,
                "entrypoint": manifest.entrypoint,
                "python_path": str(python_path.resolve()),
                "manifest_sha256": entry["manifest_sha256"],
                "source_url": entry["source_url"],
            }
        )
        self.store.request_action(manifest.id, "reload")
        return InstalledPlugin(manifest=manifest, install_dir=destination)


def _unsafe(info: zipfile.ZipInfo) -> bool:
    path = PurePosixPath(info.filename)
    if path.is_absolute() or ".." in path.parts or "\\" in info.filename:
        return True
    mode = info.external_attr >> 16
    return stat.S_ISLNK(mode)


def _plugin_template(slug: str) -> str:
    return f'''"""Generated MyBot plugin."""

from collections.abc import Mapping

from pydantic import JsonValue

from mybot.contracts import PluginManifest, PluginTaskSpec
from mybot.plugins.sdk import SimplePlugin

_config: dict[str, JsonValue] = {{"enabled": True}}


async def configure(config: Mapping[str, JsonValue]) -> None:
    _config.clear()
    _config.update(config)


async def refresh() -> None:
    """Run scheduled maintenance without blocking other plugins."""


PLUGIN = SimplePlugin(
    manifest=PluginManifest(
        id="local.{slug}",
        version="0.1.0",
        entrypoint="{slug}.plugin:PLUGIN",
        tasks=(PluginTaskSpec(id="refresh", interval_seconds=3600),),
        config_schema={{
            "type": "object",
            "properties": {{"enabled": {{"type": "boolean", "default": True}}}},
            "additionalProperties": False,
        }},
    ),
    task_handlers={{"refresh": refresh}},
    config_handler=configure,
)
'''


def _test_template(slug: str) -> str:
    return f'''from {slug}.plugin import PLUGIN


def test_manifest_and_handlers_match() -> None:
    assert PLUGIN.manifest.id == "local.{slug}"
    assert set(PLUGIN.task_handlers) == {{"refresh"}}
'''


__all__ = ["InstalledPlugin", "PluginInstaller", "scaffold_plugin"]
