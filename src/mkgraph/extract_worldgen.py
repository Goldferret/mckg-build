"""
Ore / worldgen extractor.

Resolves the 3-layer NeoForge worldgen chain into flat ore spawn records:
  ore_block → Y-range → biomes

Layer 1: biome_modifier  (data/<modid>/neoforge/biome_modifier/*.json)
  — adds a placed_feature to a set of biomes
Layer 2: placed_feature  (data/<modid>/worldgen/placed_feature/*.json)
  — references a configured_feature + Y-range/count placements
Layer 3: configured_feature (data/<modid>/worldgen/configured_feature/*.json)
  — holds the actual ore block and vein size

Each ore record:
  {
    "id":               str,   # e.g. "minecraft:ore_iron"
    "ore_block":        str,   # e.g. "minecraft:iron_ore"
    "deepslate_block":  str|None,
    "vein_size":        int,
    "discard_chance":   float,
    "min_y":            int,
    "max_y":            int,
    "biomes":           list[str],  # biome IDs or tag refs
    "mod":              str,
    "source":           str,
  }
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Iterator


def _load_json_dir(directory: Path) -> dict[str, dict]:
    """Load all JSON files in a directory tree → {namespace:name: data}."""
    result = {}
    if not directory.exists():
        return result
    for f in directory.rglob("*.json"):
        try:
            data = json.loads(f.read_text("utf-8"))
            # derive a namespaced ID from the path
            # e.g. .../worldgen/placed_feature/ore_iron.json → "ore_iron"
            name = f.stem
            result[name] = data
        except (json.JSONDecodeError, OSError):
            continue
    return result


def _extract_y_range(placements: list) -> tuple[int, int]:
    """Extract min/max Y from a placed_feature's placement list."""
    for p in placements:
        ptype = p.get("type", "")
        if "height_range" in ptype:
            height = p.get("height", {})
            min_y = max_y = 0
            # height can be {type, min_inclusive, max_inclusive} or uniform
            if isinstance(height, dict):
                # uniform or trapezoid or ... — just take min/max
                hmin = height.get("min_inclusive", height.get("min", {}))
                hmax = height.get("max_inclusive", height.get("max", {}))
                if isinstance(hmin, dict):
                    min_y = hmin.get("absolute", hmin.get("above_bottom", 0))
                elif isinstance(hmin, (int, float)):
                    min_y = int(hmin)
                if isinstance(hmax, dict):
                    max_y = hmax.get("absolute", hmax.get("below_top", 256))
                elif isinstance(hmax, (int, float)):
                    max_y = int(hmax)
            return min_y, max_y
    return -64, 320  # fallback: full world height


def _extract_biomes(biome_field) -> list[str]:
    """Extract biome IDs/tags from a biome_modifier biomes field."""
    if isinstance(biome_field, str):
        return [biome_field]
    if isinstance(biome_field, list):
        return biome_field
    if isinstance(biome_field, dict):
        # Could be a tag reference {tag: "..."} or {id: "..."}
        if "tag" in biome_field:
            return [f"#{biome_field['tag']}"]
        if "id" in biome_field:
            return [biome_field["id"]]
    return ["#minecraft:is_overworld"]  # reasonable default


def _iter_configured_features(data_dir: Path, source_label: str) -> Iterator[dict]:
    """
    Yield ore records from the data directory (vanilla or extracted mod).
    Performs the 3-layer join in-memory.
    """
    # Load all three layers
    cf_dir = data_dir / "worldgen" / "configured_feature"
    pf_dir = data_dir / "worldgen" / "placed_feature"
    bm_dir = data_dir / "neoforge" / "biome_modifier"

    if not cf_dir.exists():
        return

    configured = _load_json_dir(cf_dir)
    placed = _load_json_dir(pf_dir)
    biome_mods = _load_json_dir(bm_dir) if bm_dir.exists() else {}

    # Index placed_feature → configured_feature name
    pf_to_cf: dict[str, str] = {}
    for pf_name, pf_data in placed.items():
        feature = pf_data.get("feature", "")
        if isinstance(feature, str):
            cf_name = feature.split(":")[-1]
            pf_to_cf[pf_name] = cf_name
        elif isinstance(feature, dict):
            cf_name = feature.get("type", "").split(":")[-1]
            pf_to_cf[pf_name] = cf_name

    # Index placed_feature → biome list (from biome_modifiers)
    pf_to_biomes: dict[str, list[str]] = {}
    for bm_name, bm_data in biome_mods.items():
        bm_type = bm_data.get("type", "")
        if "add_features" not in bm_type:
            continue
        biomes = _extract_biomes(bm_data.get("biomes", []))
        features = bm_data.get("features", [])
        if isinstance(features, str):
            features = [features]
        for feat_ref in features:
            if isinstance(feat_ref, str):
                pf_name = feat_ref.split(":")[-1]
                pf_to_biomes.setdefault(pf_name, []).extend(biomes)
            elif isinstance(feat_ref, list):
                for f in feat_ref:
                    if isinstance(f, str):
                        pf_name = f.split(":")[-1]
                        pf_to_biomes.setdefault(pf_name, []).extend(biomes)

    # Yield records for ore-type configured features
    ns = data_dir.parent.name  # the modid (parent of data/)

    for cf_name, cf_data in configured.items():
        cf_type = cf_data.get("type", "")
        if "ore" not in cf_type and "scatter" not in cf_type:
            continue

        cfg = cf_data.get("config", {})
        if not isinstance(cfg, dict):
            continue
        targets = cfg.get("targets", [])
        vein_size = cfg.get("size", 0)
        discard = cfg.get("discard_chance_on_air_exposure", 0.0)

        # Extract ore blocks from targets
        ore_blocks = []
        for t in targets:
            state = t.get("state", {})
            block = state.get("Name") if isinstance(state, dict) else state
            if block:
                ore_blocks.append(block)

        if not ore_blocks:
            # Some mods use a single "state" field directly
            block = cfg.get("state", {})
            if isinstance(block, dict) and "Name" in block:
                ore_blocks = [block["Name"]]
            elif isinstance(block, str):
                ore_blocks = [block]

        if not ore_blocks:
            continue

        # Find matching placed_features and their Y-ranges + biomes
        for pf_name, cf_ref in pf_to_cf.items():
            if cf_ref != cf_name:
                continue
            pf_data = placed.get(pf_name, {})
            min_y, max_y = _extract_y_range(pf_data.get("placement", []))
            biomes = pf_to_biomes.get(pf_name, ["#minecraft:is_overworld"])
            biomes = list(dict.fromkeys(biomes))  # dedupe

            ore_block = ore_blocks[0]
            deepslate = ore_blocks[1] if len(ore_blocks) > 1 else None

            yield {
                "id":              f"{ns}:{cf_name}_{pf_name}",
                "ore_block":       ore_block,
                "deepslate_block": deepslate,
                "vein_size":       vein_size,
                "discard_chance":  discard,
                "min_y":           min_y,
                "max_y":           max_y,
                "biomes":          biomes,
                "mod":             ns,
                "source":          source_label,
            }


def _unpack_jar_data(jar_path: Path, tmp_dir: Path) -> Path | None:
    """Extract the data/<modid>/ tree from a JAR into tmp_dir/<modid>/."""
    import zipfile
    try:
        with zipfile.ZipFile(jar_path) as z:
            names = z.namelist()
            has_wg = any("worldgen" in n for n in names)
            if not has_wg:
                return None
            # Determine the primary modid
            modids = set()
            for n in names:
                p = n.split("/")
                if p[0] == "data" and len(p) >= 3:
                    if any(k in n for k in ("worldgen", "biome_modifier")):
                        modids.add(p[1])
            if not modids:
                return None
            modid = next(iter(modids))
            out_dir = tmp_dir / modid
            out_dir.mkdir(parents=True, exist_ok=True)
            prefix = f"data/{modid}/"
            for name in names:
                if name.startswith(prefix) and name.endswith(".json"):
                    rel = name[len(prefix):]
                    dest = out_dir / rel
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    dest.write_bytes(z.read(name))
            return out_dir
    except zipfile.BadZipFile:
        return None


def extract_worldgen(
    jars: list[Path],
    vanilla_dir: Path | None,
    tmp_dir: Path,
) -> list[dict]:
    """
    Extract ore worldgen records from all JARs and vanilla.
    tmp_dir is used for temporary unpacking of JAR data.
    """
    records: dict[str, dict] = {}

    # Vanilla
    if vanilla_dir:
        vanilla_data = vanilla_dir / "data" / "minecraft"
        if vanilla_data.exists():
            # vanilla mcmeta doesn't have biome_modifier (that's NeoForge-specific)
            # so vanilla ores use a different path: check worldgen/placed_feature directly
            for rec in _iter_configured_features(vanilla_data, "vanilla"):
                records[rec["id"]] = rec

    # Mods
    for jar in jars:
        data_dir = _unpack_jar_data(jar, tmp_dir)
        if data_dir is None:
            continue
        for rec in _iter_configured_features(data_dir, jar.name):
            rid = rec["id"]
            if rid not in records:
                records[rid] = rec

    return list(records.values())
