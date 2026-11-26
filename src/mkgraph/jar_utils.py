"""
Utilities for reading data files from mod JAR archives.

JARs are ZIP files. Data lives under data/<modid>/. This module handles:
  - Enumerating data files by category
  - Reading and parsing JSON safely
  - Detecting the modid(s) in a JAR from its metadata
  - Evaluating neoforge:mod_loaded conditions against the installed mod set
"""

import zipfile
import json
import re
from pathlib import Path
from typing import Iterator


# Folder names for each category — check both singular and plural
RECIPE_DIRS   = {"recipe", "recipes"}
TAG_DIRS      = {"tags"}
LOOT_DIRS     = {"loot_table", "loot_tables"}
WORLDGEN_DIRS = {"worldgen"}


def iter_data_files(jar_path: Path, subdirs: set[str]) -> Iterator[tuple[str, str, dict]]:
    """
    Yield (modid, relative_path, parsed_json) for every JSON file inside
    data/<modid>/<subdir>/... in the given JAR.

    relative_path is the full path inside the JAR e.g.
    "data/mekanism/recipe/atomic_disassembler.json"
    """
    try:
        with zipfile.ZipFile(jar_path) as z:
            for name in z.namelist():
                parts = name.split("/")
                if len(parts) < 4:
                    continue
                if parts[0] != "data":
                    continue
                modid = parts[1]
                subdir = parts[2]
                if subdir not in subdirs:
                    continue
                if not name.endswith(".json"):
                    continue
                try:
                    raw = z.read(name).decode("utf-8", errors="replace")
                    data = json.loads(raw)
                    yield modid, name, data
                except (json.JSONDecodeError, UnicodeDecodeError):
                    continue
    except zipfile.BadZipFile:
        pass


def read_modids(jar_path: Path) -> list[str]:
    """
    Return the mod IDs declared in this JAR (may be more than one for multi-mod JARs).
    Checks META-INF/neoforge.mods.toml and META-INF/mods.toml.
    Falls back to inferring from the data/ namespace list.
    """
    ids = []
    try:
        with zipfile.ZipFile(jar_path) as z:
            names_set = set(z.namelist())

            for toml_path in ("META-INF/neoforge.mods.toml", "META-INF/mods.toml"):
                if toml_path in names_set:
                    raw = z.read(toml_path).decode("utf-8", errors="replace")
                    # Simple regex — avoids a toml dependency
                    for m in re.finditer(r'modId\s*=\s*["\']([^"\']+)["\']', raw):
                        mid = m.group(1)
                        if mid not in ("examplemod",):
                            ids.append(mid)
                    if ids:
                        return list(dict.fromkeys(ids))

            # Fallback: enumerate data/ namespaces
            for name in z.namelist():
                parts = name.split("/")
                if parts[0] == "data" and len(parts) >= 3:
                    ids.append(parts[1])

    except zipfile.BadZipFile:
        pass

    return list(dict.fromkeys(ids))


def build_installed_modids(mods_dir: Path) -> set[str]:
    """
    Return the set of all mod IDs present in a mods/ directory.
    Used to evaluate neoforge:mod_loaded conditions.
    """
    ids: set[str] = set()
    for jar in mods_dir.glob("*.jar"):
        for mid in read_modids(jar):
            ids.add(mid)
    return ids


def detect_loader(mods_dir: Path) -> str:
    """
    Detect the mod loader in use from the mods/ directory.
    Returns "neoforge", "fabric", or "forge".

    Strategy:
    1. Check JAR filenames for loader-specific core JARs (fastest).
    2. Scan ALL JARs and vote by internal metadata file.
       Majority wins — avoids mis-detection from cross-loader compatibility mods
       that ship fabric.mod.json alongside Forge mods.
    3. Fall back to "neoforge" (most common for 1.21.1+).
    """
    # Pass 1: definitive filename signals — loader launcher/core JARs
    for jar in mods_dir.glob("*.jar"):
        name = jar.name.lower()
        if name.startswith("neoforge-") or name.startswith("kubejs-neoforge"):
            return "neoforge"
        if name.startswith("fabric-server-") or name.startswith("fabric-loader") or name.startswith("kubejs-fabric"):
            return "fabric"
        if name.startswith("forge-") or name.startswith("kubejs-forge"):
            return "forge"

    # Pass 2: vote across ALL JARs by metadata presence
    # neoforge.mods.toml  → neoforge
    # fabric.mod.json     → fabric
    # mods.toml (no neo)  → forge
    votes: dict[str, int] = {"neoforge": 0, "fabric": 0, "forge": 0}
    for jar in mods_dir.glob("*.jar"):
        try:
            with zipfile.ZipFile(jar) as z:
                names_set = set(z.namelist())
                if "META-INF/neoforge.mods.toml" in names_set:
                    votes["neoforge"] += 1
                elif "fabric.mod.json" in names_set:
                    votes["fabric"] += 1
                elif "META-INF/mods.toml" in names_set:
                    votes["forge"] += 1
        except zipfile.BadZipFile:
            continue

    if any(votes.values()):
        return max(votes, key=lambda k: votes[k])

    return "neoforge"  # reasonable default for modern packs


def evaluate_conditions(data: dict, installed: set[str]) -> int:
    """
    Evaluate neoforge:conditions on a recipe/trade JSON.

    Returns:
        1  — conditions absent or all passed
        0  — at least one condition failed
        2  — conditions present but could not be fully evaluated
    """
    conditions = data.get("neoforge:conditions")
    if not conditions:
        return 1  # no conditions

    result = 1
    for cond in conditions:
        ctype = cond.get("type", "")
        if ctype == "neoforge:mod_loaded":
            modid = cond.get("modid", "")
            if modid not in installed:
                return 0  # definite miss
        elif ctype in ("neoforge:true",):
            pass  # always passes
        elif ctype in ("neoforge:false",):
            return 0
        else:
            # Cannot evaluate this condition type statically
            result = 2  # unevaluated

    return result
