"""
Tag extractor.

Reads tag JSON files from mod JARs and the vanilla mcmeta directory.
Merges all contributions to each tag (replace:true is respected).

Output: a dict mapping (registry, namespace, name) → set of resolved item IDs
plus a raw list of TagRecord dicts for writing to the DB.

Tag JSON format:
  {
    "replace": false,          // optional; if true, discard prior entries
    "values": [
      "minecraft:iron_ingot",  // direct item/block/entity reference
      "#c:ingots/iron"         // nested tag reference (# prefix)
    ]
  }
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator

from mkgraph.jar_utils import iter_data_files, TAG_DIRS


@dataclass
class RawTag:
    registry: str        # "item", "block", "entity_type", etc.
    namespace: str       # e.g. "c", "minecraft", "mekanism"
    name: str            # e.g. "ingots/iron"
    replace: bool
    values: list[str]    # may include "#namespace:tagname" refs
    source: str          # jar filename or "vanilla"


def _iter_tag_files(jar_or_dir: Path, source_label: str) -> Iterator[RawTag]:
    """Yield RawTag objects from a JAR or a vanilla mcmeta data directory."""

    # Normalize plural/legacy registry directory names to canonical singular form.
    # MC 1.13-1.16 used plural dirs ("items", "blocks", "fluids", "entity_types").
    # MC 1.17+ switched to singular ("item", "block", "fluid", "entity_type").
    _REGISTRY_NORM = {
        "items":        "item",
        "blocks":       "block",
        "fluids":       "fluid",
        "entity_types": "entity_type",
        "functions":    "function",
        "game_events":  "game_event",
    }

    if jar_or_dir.is_dir():
        # Vanilla mcmeta unpacked directory: data/minecraft/tags/<registry>/<name>.json
        tags_root = jar_or_dir / "data"
        if not tags_root.exists():
            return  # Empty vanilla dir (e.g. version not in mcmeta)
        for namespace_dir in tags_root.iterdir():
            if not namespace_dir.is_dir():
                continue
            tags_dir = namespace_dir / "tags"
            if not tags_dir.exists():
                continue
            for registry_dir in tags_dir.iterdir():
                if not registry_dir.is_dir():
                    continue
                registry = _REGISTRY_NORM.get(registry_dir.name, registry_dir.name)
                for json_file in registry_dir.rglob("*.json"):
                    rel = json_file.relative_to(registry_dir)
                    name = str(rel).replace("\\", "/")[:-5]  # strip .json
                    try:
                        data = json.loads(json_file.read_text("utf-8"))
                        yield RawTag(
                            registry=registry,
                            namespace=namespace_dir.name,
                            name=name,
                            replace=data.get("replace", False),
                            values=[v for v in data.get("values", []) if isinstance(v, str)],
                            source=source_label,
                        )
                    except (json.JSONDecodeError, OSError):
                        continue
    else:
        # JAR file
        for modid, path, data in iter_data_files(jar_or_dir, TAG_DIRS):
            # path: data/<modid>/tags/<registry>/path/to/name.json
            parts = path.split("/")
            if len(parts) < 5:
                continue
            registry = _REGISTRY_NORM.get(parts[3], parts[3])
            name = "/".join(parts[4:]).replace(".json", "")
            yield RawTag(
                registry=registry,
                namespace=modid,
                name=name,
                replace=data.get("replace", False),
                values=[v for v in data.get("values", []) if isinstance(v, str)],
                source=jar_or_dir.name,
            )


def extract_tags(
    jars: list[Path],
    vanilla_dir: Path | None,
) -> list[RawTag]:
    """
    Extract all tag contributions from every JAR and the vanilla dir.
    Returns a flat list of RawTag objects in discovery order.
    Vanilla is processed first so mod entries can override with replace:true.
    """
    tags: list[RawTag] = []

    if vanilla_dir:
        tags.extend(_iter_tag_files(vanilla_dir, "vanilla"))

    for jar in jars:
        tags.extend(_iter_tag_files(jar, jar.name))

    return tags


def resolve_tags(raw_tags: list[RawTag]) -> dict[tuple[str, str, str], set[str]]:
    """
    Merge all tag contributions into a resolved map:
      (registry, full_id) → set of direct item IDs (tags expanded recursively)

    full_id = "namespace:name"

    Tags that reference other tags (#...) are left as "#namespace:name" in the
    set until a second-pass resolution (call resolve_nested below).
    """
    # First pass: merge contributions respecting replace:true
    merged: dict[tuple[str, str], list[str]] = {}  # (registry, full_id) → values

    for rt in raw_tags:
        key = (rt.registry, f"{rt.namespace}:{rt.name}")
        if rt.replace or key not in merged:
            merged[key] = list(rt.values)
        else:
            merged[key].extend(rt.values)

    # Dedupe
    return {k: set(v) for k, v in merged.items()}


def resolve_nested(
    resolved: dict[tuple[str, str], set[str]],
    max_depth: int = 10,
) -> dict[tuple[str, str], set[str]]:
    """
    Expand nested tag references (#namespace:name) to concrete item IDs.
    Modifies resolved in place and returns it.
    Items that cannot be resolved (unknown tag) are kept as-is with the # prefix.
    """
    for _ in range(max_depth):
        changed = False
        for key, values in resolved.items():
            registry = key[0]
            new_values: set[str] = set()
            for v in values:
                if v.startswith("#"):
                    ref_id = v[1:]  # strip #
                    ref_key = (registry, ref_id)
                    if ref_key in resolved:
                        new_values.update(resolved[ref_key])
                        changed = True
                    else:
                        new_values.add(v)  # keep unresolved ref
                else:
                    new_values.add(v)
            resolved[key] = new_values
        if not changed:
            break

    return resolved
