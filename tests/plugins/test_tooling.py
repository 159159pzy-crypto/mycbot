import hashlib
import io
import json
import zipfile
from pathlib import Path

import httpx
import pytest

from mybot.plugins.control import PluginControlStore
from mybot.plugins.tooling import PluginInstaller, scaffold_plugin


def test_scaffold_generates_manifest_entrypoint_and_pytest_skeleton(tmp_path: Path) -> None:
    created = scaffold_plugin("weather", tmp_path)

    manifest = json.loads(created.joinpath("plugin.json").read_text(encoding="utf-8"))
    assert manifest["id"] == "local.weather"
    assert manifest["tasks"][0]["id"] == "refresh"
    assert manifest["config_schema"]["properties"]["enabled"]["type"] == "boolean"
    assert created.joinpath("src/weather/plugin.py").exists()
    assert created.joinpath("tests/test_plugin.py").exists()
    with pytest.raises(FileExistsError):
        scaffold_plugin("weather", tmp_path)


def plugin_archive(*, unsafe: bool = False) -> tuple[bytes, bytes]:
    manifest = json.dumps(
        {
            "id": "example.weather",
            "version": "1.2.0",
            "entrypoint": "weather.plugin:PLUGIN",
            "config_schema": {"type": "object", "additionalProperties": False},
        },
        separators=(",", ":"),
    ).encode()
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("plugin.json", manifest)
        archive.writestr("src/weather/__init__.py", "")
        archive.writestr("src/weather/plugin.py", "PLUGIN = None\n")
        if unsafe:
            archive.writestr("../escape.txt", "bad")
    return buffer.getvalue(), manifest


@pytest.mark.asyncio
async def test_trusted_registry_install_verifies_hash_and_updates_managed_index(
    tmp_path: Path,
) -> None:
    payload, manifest = plugin_archive()
    registry = tmp_path / "registry.json"
    registry.write_text(
        json.dumps(
            {
                "version": 1,
                "plugins": [
                    {
                        "name": "example.weather",
                        "version": "1.2.0",
                        "source_url": "https://plugins.example/weather.zip",
                        "manifest_sha256": hashlib.sha256(manifest).hexdigest(),
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _request: httpx.Response(200, content=payload))
    ) as client:
        installer = PluginInstaller(
            registry_path=registry,
            store=PluginControlStore(tmp_path / "data"),
            client=client,
        )
        result = await installer.install("example.weather")

    assert result.manifest.id == "example.weather"
    assert result.install_dir.joinpath("plugin.json").exists()
    managed = PluginControlStore(tmp_path / "data").managed()["plugins"]
    assert managed[0]["entrypoint"] == "weather.plugin:PLUGIN"
    assert managed[0]["python_path"].endswith("src")


@pytest.mark.asyncio
async def test_registry_install_rejects_hash_mismatch_and_zip_slip(tmp_path: Path) -> None:
    payload, _manifest = plugin_archive()
    registry = tmp_path / "registry.json"
    registry.write_text(
        json.dumps(
            {
                "version": 1,
                "plugins": [
                    {
                        "name": "example.weather",
                        "version": "1.2.0",
                        "source_url": "https://plugins.example/weather.zip",
                        "manifest_sha256": "0" * 64,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _request: httpx.Response(200, content=payload))
    ) as client:
        installer = PluginInstaller(
            registry_path=registry,
            store=PluginControlStore(tmp_path / "data"),
            client=client,
        )
        with pytest.raises(ValueError, match="hash"):
            await installer.install("example.weather")

        payload, manifest = plugin_archive(unsafe=True)
        registry.write_text(
            registry.read_text().replace("0" * 64, hashlib.sha256(manifest).hexdigest()),
            encoding="utf-8",
        )
        with pytest.raises(ValueError, match="unsafe"):
            await installer.install("example.weather")
