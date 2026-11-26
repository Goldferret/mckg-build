"""
Villager trade extractor.

Sources (in merge order, later overrides earlier):
  1. VillagersTradingPlus JAR vanilla_trades/ — baseline for all vanilla professions
  2. Moonlight system in mod JARs            — data/<modid>/moonlight/villager_trade/<profession>/<trade>.json
  3. VTP's own system in mod JARs            — data/<modid>/villager_trades/<profession>.json
                                               and data/<modid>/default_villager_trades/<profession>.json

Each trade record:
  {
    "profession":  str,          # e.g. "minecraft:farmer"
    "tier":        str,          # novice|apprentice|journeyman|expert|master
    "type":        str,          # trade type e.g. "villagertradingplus:sell_item"
    "price_item":  str|None,     # what the villager wants
    "price_count": int,
    "offer_item":  str|None,     # what the villager gives
    "offer_count": int,
    "max_uses":    int,
    "mod":         str,          # source mod
    "source":      str,          # "vanilla_vtp", jar filename, etc.
    "raw":         dict,         # full trade JSON for complex types
  }
"""

from __future__ import annotations

import json
import zipfile
from pathlib import Path
from typing import Iterator


VANILLA_TIERS = ["novice", "apprentice", "journeyman", "expert", "master"]


def _normalise_item(obj) -> tuple[str | None, int]:
    """Extract (item_id, count) from various trade item formats."""
    if obj is None:
        return None, 1
    if isinstance(obj, str):
        return obj, 1
    if isinstance(obj, dict):
        item = obj.get("item") or obj.get("id") or obj.get("tag")
        if item and not item.startswith("#") and "tag" not in obj:
            pass
        elif "tag" in obj:
            item = f"#{obj['tag']}"
        return item, int(obj.get("count", 1))
    return None, 1


def _normalise_vtp_trade(trade: dict, profession: str, tier: str, source: str) -> dict:
    """Normalise a VTP-format trade object to a flat record."""
    ttype = trade.get("type", "unknown")

    # sell_item / sell_*
    offer_item, offer_count = _normalise_item(trade.get("sell") or trade.get("reward") or trade.get("offer"))
    price_item, price_count = _normalise_item(trade.get("priceIn") or trade.get("price") or trade.get("buy"))

    return {
        "profession":  profession,
        "tier":        tier,
        "type":        ttype,
        "price_item":  price_item,
        "price_count": price_count,
        "offer_item":  offer_item,
        "offer_count": offer_count,
        "max_uses":    trade.get("max_uses", trade.get("maxUses", 12)),
        "mod":         profession.split(":")[0] if ":" in profession else "minecraft",
        "source":      source,
        "raw":         trade,
    }


def _load_vtp_vanilla(vtp_jar: Path) -> list[dict]:
    """Load vanilla trade templates from the VillagersTradingPlus JAR."""
    records = []
    try:
        with zipfile.ZipFile(vtp_jar) as z:
            for name in z.namelist():
                if "vanilla_trades" not in name or not name.endswith(".json"):
                    continue
                try:
                    data = json.loads(z.read(name).decode("utf-8"))
                    profession = data.get("profession", "minecraft:unknown")
                    for tier in VANILLA_TIERS:
                        for trade in data.get("trades", {}).get(tier, []):
                            records.append(_normalise_vtp_trade(trade, profession, tier, "vanilla_vtp"))
                except (json.JSONDecodeError, KeyError):
                    continue
    except zipfile.BadZipFile:
        pass
    return records


def _load_moonlight_trades(jars: list[Path]) -> list[dict]:
    """Load Moonlight-system trades from mod JARs."""
    records = []
    for jar in jars:
        try:
            with zipfile.ZipFile(jar) as z:
                for name in z.namelist():
                    if "moonlight/villager_trade" not in name or not name.endswith(".json"):
                        continue
                    # path: data/<ns>/moonlight/villager_trade/<profession>/<trade>.json
                    parts = name.split("/")
                    if len(parts) < 6:
                        continue
                    ns = parts[1]            # data namespace (profession owner)
                    profession_name = parts[4]  # e.g. "carpenter", "cartographer"
                    try:
                        data = json.loads(z.read(name).decode("utf-8"))
                        # Moonlight stores individual trades, not a list
                        ttype  = data.get("type", "simple")
                        level  = data.get("level", 1)
                        tier   = VANILLA_TIERS[min(level - 1, 4)]
                        offer_item, offer_count = _normalise_item(data.get("offer"))
                        price_item, price_count = _normalise_item(data.get("price"))
                        mod = jar.stem.split("-")[0]  # jar filename without version
                        profession = f"{ns}:{profession_name}"

                        records.append({
                            "profession":  profession,
                            "tier":        tier,
                            "type":        f"moonlight:{ttype}",
                            "price_item":  price_item,
                            "price_count": price_count,
                            "offer_item":  offer_item,
                            "offer_count": offer_count,
                            "max_uses":    data.get("max_trades", 12),
                            "mod":         mod,
                            "source":      jar.name,
                            "raw":         data,
                        })
                    except (json.JSONDecodeError, KeyError):
                        continue
        except zipfile.BadZipFile:
            continue
    return records


def _load_vtp_mod_trades(jars: list[Path]) -> list[dict]:
    """
    Load trades defined via VTP's own system in mod JARs:
      - data/<modid>/villager_trades/<profession>.json        (additive)
      - data/<modid>/default_villager_trades/<profession>.json (replace)
    """
    records = []
    for jar in jars:
        try:
            with zipfile.ZipFile(jar) as z:
                for name in z.namelist():
                    is_replace = "default_villager_trades" in name
                    is_add     = "villager_trades" in name and "default_" not in name
                    if not (is_replace or is_add):
                        continue
                    if not name.endswith(".json"):
                        continue
                    try:
                        data = json.loads(z.read(name).decode("utf-8"))
                        profession = data.get("profession", "unknown")
                        source = f"{jar.name}:{'replace' if is_replace else 'add'}"
                        for tier in VANILLA_TIERS:
                            for trade in data.get("trades", {}).get(tier, []):
                                records.append(_normalise_vtp_trade(trade, profession, tier, source))
                    except (json.JSONDecodeError, KeyError):
                        continue
        except zipfile.BadZipFile:
            continue
    return records


def extract_trades(jars: list[Path], vtp_jar: Path | None) -> list[dict]:
    """
    Return all trade records, vanilla first then mod additions.
    Replace semantics are handled at the DB write stage.
    """
    records = []
    if vtp_jar:
        records.extend(_load_vtp_vanilla(vtp_jar))
    records.extend(_load_moonlight_trades(jars))
    records.extend(_load_vtp_mod_trades(jars))
    return records
