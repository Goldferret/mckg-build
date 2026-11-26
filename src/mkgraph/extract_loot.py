"""
Loot table extractor.

Covers both mob drops and structure/chest loot since they use the same
JSON format. The category is inferred from the path:
  loot_table/entities/<mob>.json  → category "mob"
  loot_table/blocks/<block>.json  → category "block"
  loot_table/chests/<name>.json   → category "chest"
  loot_table/<other>/...          → category "other"

Each loot record:
  {
    "id":       str,   # e.g. "minecraft:entities/zombie"
    "category": str,   # "mob", "chest", "block", "other"
    "source_id": str,  # e.g. "minecraft:zombie"  (inferred from path)
    "mod":      str,
    "drops":    list,  # list of {item, min_count, max_count, conditions}
    "data":     dict,  # full raw loot table JSON
    "source":   str,   # jar filename or "vanilla"
  }

We do a best-effort item extraction from the loot table's pool entries.
Vanilla loot tables can be complex (nested pools, conditions, functions),
so we extract what we can and store the full JSON for cases we cannot parse.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Iterator

from mkgraph.jar_utils import iter_data_files, LOOT_DIRS


_CATEGORY_MAP = {
    "entities": "mob",
    "entity":   "mob",
    "blocks":   "block",
    "block":    "block",
    "chests":   "chest",
    "chest":    "chest",
}


def _unwrap_bound(val) -> int:
    """
    Extract an integer from a Y-bound value, which may be:
      3                               → 3
      {"absolute": 3}                 → 3
      {"above_bottom": 3}             → 3  (relative to world bottom, treat as approx)
      {"below_top": 3}                → 256 - 3 (approximate)
    Returns 1 as a safe fallback.
    """
    if isinstance(val, (int, float)):
        return int(val)
    if isinstance(val, dict):
        if "absolute" in val:
            return int(val["absolute"])
        if "above_bottom" in val:
            return int(val["above_bottom"])          # approximate
        if "below_top" in val:
            return max(0, 320 - int(val["below_top"]))  # approximate
    return 1


def _extract_drops(data: dict) -> list[dict]:
    """
    Best-effort extraction of drop items from a loot table JSON.
    Returns a list of {item, min_count, max_count, conditions} dicts.
    """
    drops = []

    for pool in data.get("pools", []):
        for entry in pool.get("entries", []):
            _extract_entry(entry, drops)

    return drops


def _extract_entry(entry: dict, drops: list):
    """Recursively extract items from a loot entry."""
    etype = entry.get("type", "")

    if etype in ("minecraft:item", "item"):
        item_name = entry.get("name", "")
        if not item_name:
            return
        min_count = max_count = 1
        conditions = []
        for fn in entry.get("functions", []):
            fn_type = fn.get("function", fn.get("type", ""))
            if "count" in fn_type or "set_count" in fn_type:
                cnt = fn.get("count", {})
                if isinstance(cnt, (int, float)):
                    min_count = max_count = int(cnt)
                elif isinstance(cnt, dict):
                    # count may be a range dict, possibly with nested bound objects
                    raw_min = cnt.get("min", 1)
                    raw_max = cnt.get("max", 1)
                    min_count = _unwrap_bound(raw_min)
                    max_count = _unwrap_bound(raw_max)
        for cond in entry.get("conditions", []):
            ctype = cond.get("condition", cond.get("type", ""))
            conditions.append(ctype)
        drops.append({
            "item":       item_name,
            "min_count":  min_count,
            "max_count":  max_count,
            "conditions": conditions,
        })

    elif etype in ("minecraft:alternatives", "alternatives",
                   "minecraft:group", "group",
                   "minecraft:sequence", "sequence"):
        for child in entry.get("children", []):
            _extract_entry(child, drops)

    elif etype in ("minecraft:tag", "tag"):
        # Tag-based drops — record as tag reference
        tag = entry.get("name", "")
        if tag:
            drops.append({
                "item":       f"#{tag}",
                "min_count":  1,
                "max_count":  1,
                "conditions": [],
            })


def _iter_loot_files(jar_or_dir: Path, source_label: str) -> Iterator[dict]:
    """Yield loot records from a JAR or vanilla mcmeta directory."""

    if jar_or_dir.is_dir():
        loot_root = jar_or_dir / "data"
        if not loot_root.exists():
            return  # Empty vanilla dir (e.g. version not in mcmeta)
        for ns_dir in loot_root.iterdir():
            if not ns_dir.is_dir():
                continue
            for loot_dir_name in ("loot_table", "loot_tables"):
                loot_dir = ns_dir / loot_dir_name
                if not loot_dir.exists():
                    continue
                for json_file in loot_dir.rglob("*.json"):
                    rel = json_file.relative_to(loot_dir)
                    # Full path relative to loot_table/ — matches the JAR reader format
                    # e.g. "entities/zombie" so mob drops != chest drops of same name.
                    name = str(rel).replace("\\", "/")[:-5]  # strip .json
                    category_key = rel.parts[0] if rel.parts else ""
                    category = _CATEGORY_MAP.get(category_key, "other")
                    full_id = f"{ns_dir.name}:{name}"
                    try:
                        data = json.loads(json_file.read_text("utf-8"))
                        yield {
                            "id":        full_id,
                            "category":  category,
                            "source_id": full_id,
                            "mod":       ns_dir.name,
                            "drops":     _extract_drops(data),
                            "data":      data,
                            "source":    source_label,
                        }
                    except (json.JSONDecodeError, OSError):
                        continue
    else:
        for modid, path, data in iter_data_files(jar_or_dir, LOOT_DIRS):
            parts = path.split("/")
            # data/<modid>/loot_table/<category>/<name...>.json
            if len(parts) < 5:
                continue
            category_key = parts[3]
            category = _CATEGORY_MAP.get(category_key, "other")
            # Include the full path from category onwards so the ID matches
            # the directory reader: "entities/zombie", not just "zombie".
            name = "/".join(parts[3:]).replace(".json", "")
            full_id = f"{modid}:{name}"
            yield {
                "id":        full_id,
                "category":  category,
                "source_id": full_id,
                "mod":       modid,
                "drops":     _extract_drops(data),
                "data":      data,
                "source":    jar_or_dir.name,
            }


def extract_loot(jars: list[Path], vanilla_dir: Path | None) -> list[dict]:
    """Extract all loot tables. Vanilla first, then mods."""
    seen: dict[str, dict] = {}

    if vanilla_dir:
        for rec in _iter_loot_files(vanilla_dir, "vanilla"):
            seen[rec["id"]] = rec

    for jar in jars:
        for rec in _iter_loot_files(jar, jar.name):
            rid = rec["id"]
            if rid in seen:
                seen[rid]["source"] = "vanilla+jar" if seen[rid]["source"] == "vanilla" else "jar"
            else:
                seen[rid] = rec

    return list(seen.values())
