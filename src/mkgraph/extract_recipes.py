"""
Recipe extractor.

Two sources, in priority order:
  1. KubeJS dump (authoritative post-KubeJS state)      source="dump"
  2. Static JAR scan                                      source="jar"

Merge rule: if a recipe ID exists in both, the dump wins.

Each recipe record:
  {
    "id":              str,       # e.g. "minecraft:iron_sword"
    "type":            str,       # e.g. "minecraft:crafting_shaped"
    "mod":             str,       # namespace of the recipe ID
    "result_item":     str|None,  # resolved output item ID
    "result_count":    int,       # output count (default 1)
    "ingredients":     list,      # list of {kind:"item"|"tag", value:str}
    "data":            dict,      # full raw recipe JSON
    "source":          str,       # "dump", "jar", or "dump+jar"
    "conditions_met":  int,       # 1=yes, 0=no, 2=unevaluated
  }
"""

from __future__ import annotations

import json
import glob
from pathlib import Path
from typing import Iterator

from mkgraph.jar_utils import iter_data_files, RECIPE_DIRS, evaluate_conditions


# ---------------------------------------------------------------------------
# Result extraction
# ---------------------------------------------------------------------------

def _first_item_entry(lst: list, count_key: str = "count") -> tuple[str | None, int]:
    """
    Given a list of output entry dicts, return (item_id, count) for the
    primary output.

    Rules:
    - Prefer item entries (have "item" or "id" key) over fluid entries.
    - Among items: prefer first entry with chance absent or >= 1.0.
    - If no item entries exist, fall back to the first fluid entry and return
      a "fluid:<id>" synthetic item ID so fluid-producing recipes stay tracked.
    """
    item_entries = [r for r in lst if isinstance(r, dict) and ("item" in r or "id" in r)]
    primary = next(
        (r for r in item_entries if r.get("chance", 1.0) >= 1.0),
        item_entries[0] if item_entries else None,
    )
    if primary is not None:
        item = primary.get("item") or primary.get("id")
        if not isinstance(item, str):
            return None, 1
        raw_count = primary.get(count_key, primary.get("count", 1))
        if not isinstance(raw_count, int):
            try:
                raw_count = int(raw_count)
            except (TypeError, ValueError):
                raw_count = 1
        return item, raw_count

    # No item entries — fall back to first fluid entry
    fluid_entries = [r for r in lst if isinstance(r, dict) and "fluid" in r]
    if fluid_entries:
        fluid_id = fluid_entries[0].get("fluid")
        if isinstance(fluid_id, str):
            raw_count = fluid_entries[0].get("amount", fluid_entries[0].get(count_key, 1))
            if not isinstance(raw_count, int):
                try:
                    raw_count = int(raw_count)
                except (TypeError, ValueError):
                    raw_count = 1
            return f"fluid:{fluid_id}", raw_count

    return None, 1


def _conditions_met(conditions: list, installed: set[str]) -> bool:
    """Check whether a list of forge-style recipe conditions are satisfied."""
    for cond in conditions:
        ctype = cond.get("type", "")
        if ctype == "forge:mod_loaded":
            if cond.get("modid", "") not in installed:
                return False
        elif ctype == "forge:not":
            inner = cond.get("value", {})
            if inner.get("type") == "forge:mod_loaded":
                if inner.get("modid", "") in installed:
                    return False
        # Other condition types: conservatively assume True
    return True


def _unwrap_conditional(data: dict, installed: set[str]) -> dict | None:
    """
    Unwrap a forge:conditional recipe wrapper.
    Returns the inner recipe dict of the first entry whose conditions are
    satisfied, or None if no entry qualifies (recipe should be skipped).
    """
    for entry in data.get("recipes", []):
        if _conditions_met(entry.get("conditions", []), installed):
            return entry.get("recipe")
    return None


def _get_result(data: dict) -> tuple[str | None, int]:
    """Return (item_id, count) from a recipe's result field.

    Priority order (first match wins):
    1. result: "str"                      vanilla string shorthand
    2. result: {item/id, count}           vanilla + most mods
    3. result: {fluid, amount}            fluid-producing recipes (→ "fluid:<id>")
    4. result: [{item, count, chance}]    Thermal:*, FarmersDelight (result is array)
    5. results: [{item, count, chance}]   Create:* and other processing mods
    6. item_outputs: [{item, amount}]     Modern Industrialization
    7. output: {item, count/amount}       Botania mana_infusion and similar
    8. output: {fluid, amount}            Fluid output via "output" key
    """
    res = data.get("result")
    if res is not None:
        if isinstance(res, str):
            return res, 1
        if isinstance(res, dict):
            item = res.get("item") or res.get("id")
            if isinstance(item, str):
                count = res.get("count", 1)
                if not isinstance(count, int):
                    try:
                        count = int(count)
                    except (TypeError, ValueError):
                        count = 1
                return item, count
            # Tag-output result (TiC casting, ore-dict unification, etc.)
            tag = res.get("tag")
            if isinstance(tag, str):
                return f"tag:{tag}", 1
            # Not an item — check for fluid output (e.g. tconstruct:melting)
            fluid = res.get("fluid")
            if isinstance(fluid, str):
                count = res.get("amount", res.get("count", 1))
                if not isinstance(count, int):
                    try:
                        count = int(count)
                    except (TypeError, ValueError):
                        count = 1
                return f"fluid:{fluid}", count
            # Result dict has no item, tag, or fluid key
            return None, 1
        if isinstance(res, list):
            # Thermal:*, FarmersDelight: result is an array of output entries
            return _first_item_entry(res)

    # Create:*, other processing mods with plural "results" key
    results = data.get("results")
    if isinstance(results, list):
        return _first_item_entry(results)

    # Modern Industrialization: item_outputs with "amount" as count key
    item_outputs = data.get("item_outputs")
    if isinstance(item_outputs, list):
        return _first_item_entry(item_outputs, count_key="amount")

    # Botania mana_infusion and similar: singular "output" dict
    output = data.get("output")
    if isinstance(output, dict):
        item = output.get("item") or output.get("id")
        if isinstance(item, str):
            count = output.get("count", output.get("amount", 1))
            if not isinstance(count, int):
                try:
                    count = int(count)
                except (TypeError, ValueError):
                    count = 1
            return item, count
        # Fluid output via "output" key
        fluid = output.get("fluid")
        if isinstance(fluid, str):
            count = output.get("amount", output.get("count", 1))
            if not isinstance(count, int):
                try:
                    count = int(count)
                except (TypeError, ValueError):
                    count = 1
            return f"fluid:{fluid}", count

    return None, 1


# ---------------------------------------------------------------------------
# Ingredient extraction
# ---------------------------------------------------------------------------

def _extract_ingredients(data: dict) -> list[dict]:
    """
    Return a list of {kind, value} dicts for each unique ingredient.
    kind is "item" or "tag".
    """
    pairs: list[dict] = []
    seen: set[tuple] = set()

    def _add(kind: str, value: str):
        k = (kind, value)
        if k not in seen:
            seen.add(k)
            pairs.append({"kind": kind, "value": value})

    def _extract(obj):
        if isinstance(obj, dict):
            if "item" in obj:
                val = obj["item"]
                # Some mods use {"item": "#namespace:tag"} as a tag shorthand
                if isinstance(val, str) and val.startswith("#"):
                    _add("tag", val[1:])
                else:
                    _add("item", val)
            elif "id" in obj:
                val = obj["id"]
                if isinstance(val, str) and val.startswith("#"):
                    _add("tag", val[1:])
                else:
                    _add("item", val)
            elif "tag" in obj:
                _add("tag", obj["tag"])
        elif isinstance(obj, str):
            if obj.startswith("#"):
                _add("tag", obj[1:])
            else:
                _add("item", obj)
        elif isinstance(obj, list):
            for x in obj:
                _extract(x)

    rtype = data.get("type", "")
    if "shaped" in rtype:
        for v in data.get("key", {}).values():
            _extract(v)
    elif "shapeless" in rtype:
        _extract(data.get("ingredients", []))
    elif rtype in (
        "minecraft:smelting", "minecraft:blasting",
        "minecraft:smoking", "minecraft:campfire_cooking",
    ):
        _extract(data.get("ingredient", {}))
    else:
        # Generic fallback: try both common field names
        _extract(data.get("ingredients", data.get("ingredient", [])))

    return pairs


# ---------------------------------------------------------------------------
# KubeJS dump loader
# ---------------------------------------------------------------------------

def _load_dump(dump_dir: Path) -> dict[str, dict]:
    """Load all recipes from a KubeJS dump directory. Returns {id: record}."""
    records: dict[str, dict] = {}
    for chunk in sorted(dump_dir.glob("recipes_*.json")):
        if "meta" in chunk.name:
            continue
        try:
            for entry in json.loads(chunk.read_text("utf-8")):
                rid = entry.get("id", "")
                if not rid:
                    continue
                # Support both wrapped {id, data:{...}} and flat {id, result, key...}
                # MC 1.21+ dumps may omit the "data" wrapper entirely.
                data = entry.get("data") or entry
                result_item, result_count = _get_result(data)
                mod = rid.split(":")[0] if ":" in rid else "unknown"
                records[rid] = {
                    "id":             rid,
                    "type":           entry.get("type", data.get("type", "unknown")),
                    "mod":            mod,
                    "result_item":    result_item,
                    "result_count":   result_count,
                    "ingredients":    _extract_ingredients(data),
                    "data":           data,
                    "source":         "dump",
                    "conditions_met": 1,  # dump = server already evaluated conditions
                }
        except (json.JSONDecodeError, OSError):
            continue
    return records


# ---------------------------------------------------------------------------
# JAR scanner
# ---------------------------------------------------------------------------

def _iter_jar_recipes(
    jars: list[Path],
    installed: set[str],
) -> Iterator[dict]:
    """Yield recipe records from all JARs."""
    for jar in jars:
        for modid, path, data in iter_data_files(jar, RECIPE_DIRS):
            # Derive a recipe ID from the path: data/<ns>/recipe/.../name.json
            parts = path.split("/")
            # parts[0]=data, parts[1]=namespace, parts[2]=recipe|recipes, parts[3..]=path
            ns = parts[1]
            name_parts = parts[3:]
            recipe_name = "/".join(name_parts).replace(".json", "")
            rid = f"{ns}:{recipe_name}"

            rtype = data.get("type", "unknown")

            # Unwrap forge:conditional / porting_lib:conditional — replace outer
            # envelope with the first inner recipe whose conditions are met;
            # skip if none qualify.
            if rtype in ("forge:conditional", "porting_lib:conditional"):
                inner = _unwrap_conditional(data, installed)
                if inner is None:
                    continue  # No conditions satisfied — skip entirely
                data  = inner
                rtype = data.get("type", "unknown")

            result_item, result_count = _get_result(data)
            conditions_met = evaluate_conditions(data, installed)
            mod = rid.split(":")[0] if ":" in rid else "unknown"

            yield {
                "id":             rid,
                "type":           rtype,
                "mod":            mod,
                "result_item":    result_item,
                "result_count":   result_count,
                "ingredients":    _extract_ingredients(data),
                "data":           data,
                "source":         "jar",
                "conditions_met": conditions_met,
            }


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def extract_recipes(
    jars: list[Path],
    installed: set[str],
    dump_dir: Path | None = None,
) -> list[dict]:
    """
    Return a merged list of recipe records.
    Dump is loaded first, JAR records fill in anything not in the dump.
    """
    # Start with dump (highest priority)
    records: dict[str, dict] = {}
    if dump_dir:
        records = _load_dump(dump_dir)

    # Overlay JAR recipes — only add if not already in dump
    for rec in _iter_jar_recipes(jars, installed):
        rid = rec["id"]
        if rid in records:
            # Mark as confirmed by both sources
            records[rid]["source"] = "dump+jar"
        else:
            records[rid] = rec

    return list(records.values())
