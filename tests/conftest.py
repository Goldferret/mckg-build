"""
Shared pytest fixtures.

jar_factory  — creates a real on-disk .jar (ZIP) file in tmp_path.
               Accepts a dict of {archive_path: payload} where payload is
               a str/bytes or any JSON-serializable object.
"""

import json
import zipfile
from pathlib import Path
from typing import Any

import pytest


@pytest.fixture
def jar_factory(tmp_path: Path):
    def make(name: str, members: dict[str, Any]) -> Path:
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(path, "w") as z:
            for member_path, payload in members.items():
                if isinstance(payload, (str, bytes)):
                    z.writestr(member_path, payload)
                else:
                    z.writestr(member_path, json.dumps(payload))
        return path

    return make


@pytest.fixture
def vanilla_dir(tmp_path: Path):
    """
    Returns a factory that creates a minimal vanilla mcmeta directory structure.
    Usage: vanilla_dir({"item/ingots/iron.json": {"values": ["minecraft:iron_ingot"]}})
    Returns the base data directory (the one containing data/).
    """
    def make(tag_files: dict[str, Any] | None = None,
             loot_files: dict[str, Any] | None = None) -> Path:
        base = tmp_path / "vanilla"
        base.mkdir(parents=True, exist_ok=True)
        if tag_files:
            for rel_path, content in tag_files.items():
                # rel_path is relative to data/minecraft/tags/
                dest = base / "data" / "minecraft" / "tags" / rel_path
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_text(json.dumps(content), encoding="utf-8")
        if loot_files:
            for rel_path, content in loot_files.items():
                # rel_path is relative to data/minecraft/loot_table/
                dest = base / "data" / "minecraft" / "loot_table" / rel_path
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_text(json.dumps(content), encoding="utf-8")
        return base

    return make
