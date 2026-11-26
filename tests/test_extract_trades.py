"""
Tests for mkgraph.extract_trades.

Covers:
  1. Moonlight 6-part path is accepted; 5-part path is rejected
  2. VTP field alias combinations (all 9 offer × price combos)
  3. Item format normalization and defaults
  4. Alias precedence — first alias wins
  5. VTP vanilla template loader (end-to-end JAR read)
  6. VTP mod loaders — villager_trades and default_villager_trades
"""

import json
import zipfile
import pytest
from mkgraph.extract_trades import (
    _normalise_vtp_trade,
    _normalise_item,
    _load_moonlight_trades,
    _load_vtp_vanilla,
    _load_vtp_mod_trades,
    extract_trades,
)


# ---------------------------------------------------------------------------
# 1. Moonlight path length gate
# ---------------------------------------------------------------------------

def test_moonlight_6part_path_accepted(jar_factory):
    jar = jar_factory("demo-1.0.jar", {
        "data/demo/moonlight/villager_trade/carpenter/planks.json": {
            "type": "simple",
            "level": 2,
            "offer": {"id": "demo:plank", "count": 8},
            "price": {"item": "minecraft:emerald", "count": 1},
            "max_trades": 6,
        }
    })
    trades = _load_moonlight_trades([jar])
    assert len(trades) == 1
    t = trades[0]
    assert t["profession"]  == "demo:carpenter"
    assert t["tier"]        == "apprentice"
    assert t["type"]        == "moonlight:simple"
    assert t["offer_item"]  == "demo:plank"
    assert t["offer_count"] == 8
    assert t["price_item"]  == "minecraft:emerald"
    assert t["price_count"] == 1
    assert t["max_uses"]    == 6
    assert t["source"]      == "demo-1.0.jar"


def test_moonlight_5part_path_rejected(jar_factory):
    jar = jar_factory("demo-1.0.jar", {
        "data/demo/moonlight/villager_trade/planks.json": {
            "type": "simple", "level": 1,
        }
    })
    assert _load_moonlight_trades([jar]) == []


def test_moonlight_7part_path_accepted(jar_factory):
    """Deeper nesting (7+ parts) is also valid — the guard is < 6, not == 6."""
    jar = jar_factory("demo-1.0.jar", {
        "data/demo/moonlight/villager_trade/carpenter/sub/item.json": {
            "type": "simple",
            "level": 1,
            "offer": {"item": "demo:item"},
            "price": {"item": "minecraft:emerald"},
        }
    })
    # Should not raise; whether the profession name is right is secondary
    trades = _load_moonlight_trades([jar])
    assert len(trades) == 1


# ---------------------------------------------------------------------------
# 2. VTP field alias combinations
# ---------------------------------------------------------------------------

OFFER_ITEM = {"id": "demo:bread", "count": 3}
PRICE_ITEM = {"item": "minecraft:emerald", "count": 2}

@pytest.mark.parametrize("offer_key", ["sell", "reward", "offer"])
@pytest.mark.parametrize("price_key", ["priceIn", "price", "buy"])
def test_vtp_field_aliases(offer_key, price_key):
    trade = {offer_key: OFFER_ITEM, price_key: PRICE_ITEM}
    rec = _normalise_vtp_trade(trade, "minecraft:farmer", "novice", "test")
    assert rec["offer_item"]  == "demo:bread"
    assert rec["offer_count"] == 3
    assert rec["price_item"]  == "minecraft:emerald"
    assert rec["price_count"] == 2
    assert rec["profession"]  == "minecraft:farmer"
    assert rec["tier"]        == "novice"
    assert rec["source"]      == "test"
    assert rec["raw"]         == trade    # value equality, not identity


# ---------------------------------------------------------------------------
# 3. Item format normalization
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("obj,expected_item,expected_count", [
    ("demo:sword",                              "demo:sword", 1),
    ({"item": "demo:sword"},                   "demo:sword", 1),
    ({"id":   "demo:sword"},                   "demo:sword", 1),
    ({"item": "demo:sword", "count": 5},       "demo:sword", 5),
    ({"tag": "c:ingots/iron", "count": 2},     "#c:ingots/iron", 2),
    (None,                                      None,          1),
])
def test_normalise_item(obj, expected_item, expected_count):
    item, count = _normalise_item(obj)
    assert item  == expected_item
    assert count == expected_count


def test_missing_offer_and_price_produce_none():
    rec = _normalise_vtp_trade({}, "minecraft:farmer", "novice", "test")
    assert rec["offer_item"]  is None
    assert rec["price_item"]  is None
    assert rec["offer_count"] == 1
    assert rec["price_count"] == 1


@pytest.mark.parametrize("field,key", [("max_uses", 6), ("maxUses", 8)])
def test_max_uses_both_spellings(field, key):
    rec = _normalise_vtp_trade({field: key}, "minecraft:farmer", "novice", "test")
    assert rec["max_uses"] == key


def test_max_uses_default_is_12():
    rec = _normalise_vtp_trade({}, "minecraft:farmer", "novice", "test")
    assert rec["max_uses"] == 12


# ---------------------------------------------------------------------------
# 4. Alias precedence
# ---------------------------------------------------------------------------

def test_offer_precedence_sell_wins():
    trade = {
        "sell":   {"item": "demo:sell",   "count": 1},
        "reward": {"item": "demo:reward", "count": 1},
        "offer":  {"item": "demo:offer",  "count": 1},
    }
    rec = _normalise_vtp_trade(trade, "minecraft:farmer", "novice", "test")
    assert rec["offer_item"] == "demo:sell"


def test_price_precedence_priceIn_wins():
    trade = {
        "priceIn": {"item": "demo:priceIn", "count": 1},
        "price":   {"item": "demo:price",   "count": 1},
        "buy":     {"item": "demo:buy",     "count": 1},
    }
    rec = _normalise_vtp_trade(trade, "minecraft:farmer", "novice", "test")
    assert rec["price_item"] == "demo:priceIn"


def test_empty_primary_falls_back_to_secondary():
    """An empty dict is falsy, so the next alias is used."""
    trade = {"sell": {}, "reward": {"item": "demo:fallback", "count": 1}}
    rec = _normalise_vtp_trade(trade, "minecraft:farmer", "novice", "test")
    assert rec["offer_item"] == "demo:fallback"


# ---------------------------------------------------------------------------
# 5. VTP vanilla template loader (real JAR)
# ---------------------------------------------------------------------------

FARMER_VTP = {
    "profession": "minecraft:farmer",
    "trades": {
        "novice":      [{"type": "villagertradingplus:buy_item",
                          "buy": {"item": "minecraft:wheat", "count": 20},
                          "reward": {"item": "minecraft:emerald", "count": 1}}],
        "apprentice":  [],
        "journeyman":  [],
        "expert":      [],
        "master":      [],
    }
}


def test_vtp_vanilla_loader(jar_factory, tmp_path):
    vtp_jar = jar_factory("vtp.jar", {
        "data/villagertradingplus/vanilla_trades/farmer.json": FARMER_VTP,
    })
    trades = _load_vtp_vanilla(vtp_jar)
    assert len(trades) == 1
    t = trades[0]
    assert t["profession"]  == "minecraft:farmer"
    assert t["tier"]        == "novice"
    assert t["price_item"]  == "minecraft:wheat"
    assert t["price_count"] == 20
    assert t["offer_item"]  == "minecraft:emerald"
    assert t["source"]      == "vanilla_vtp"


def test_vtp_vanilla_loader_empty_tiers(jar_factory):
    """Tiers with no trades must be silently skipped."""
    vtp_jar = jar_factory("vtp.jar", {
        "data/villagertradingplus/vanilla_trades/farmer.json": {
            "profession": "minecraft:farmer",
            "trades": {"novice": [], "apprentice": [], "journeyman": [], "expert": [], "master": []},
        }
    })
    assert _load_vtp_vanilla(vtp_jar) == []


# ---------------------------------------------------------------------------
# 6. VTP mod loaders — villager_trades and default_villager_trades
# ---------------------------------------------------------------------------

MOD_ADD_TRADE = {
    "profession": "demo:baker",
    "trades": {
        "novice": [{"type": "villagertradingplus:sell_item",
                    "sell": {"item": "demo:bread", "count": 1},
                    "priceIn": {"item": "minecraft:emerald", "count": 2}}],
        "apprentice": [], "journeyman": [], "expert": [], "master": [],
    }
}

MOD_REPLACE_TRADE = {
    "profession": "minecraft:farmer",
    "trades": {
        "novice": [{"type": "villagertradingplus:sell_item",
                    "sell": {"item": "demo:special_food", "count": 1},
                    "priceIn": {"item": "minecraft:emerald", "count": 5}}],
        "apprentice": [], "journeyman": [], "expert": [], "master": [],
    }
}


def test_vtp_mod_add_trades(jar_factory):
    jar = jar_factory("demo.jar", {
        "data/demo/villager_trades/baker.json": MOD_ADD_TRADE,
    })
    trades = _load_vtp_mod_trades([jar])
    assert len(trades) == 1
    assert trades[0]["profession"]  == "demo:baker"
    assert trades[0]["offer_item"]  == "demo:bread"
    assert "add" in trades[0]["source"]


def test_vtp_mod_replace_trades(jar_factory):
    jar = jar_factory("demo.jar", {
        "data/demo/default_villager_trades/farmer.json": MOD_REPLACE_TRADE,
    })
    trades = _load_vtp_mod_trades([jar])
    assert len(trades) == 1
    assert trades[0]["profession"]  == "minecraft:farmer"
    assert trades[0]["offer_item"]  == "demo:special_food"
    assert "replace" in trades[0]["source"]


def test_extract_trades_combines_sources(jar_factory, tmp_path):
    """extract_trades() combines VTP vanilla, Moonlight, and VTP mod trades."""
    vtp_jar = jar_factory("vtp.jar", {
        "data/villagertradingplus/vanilla_trades/farmer.json": FARMER_VTP,
    })
    mod_jar = jar_factory("demo.jar", {
        "data/demo/moonlight/villager_trade/carpenter/planks.json": {
            "type": "simple", "level": 1,
            "offer": {"item": "demo:plank", "count": 4},
            "price": {"item": "minecraft:emerald", "count": 1},
        },
        "data/demo/villager_trades/baker.json": MOD_ADD_TRADE,
    })

    trades = extract_trades([vtp_jar, mod_jar], vtp_jar=vtp_jar)
    sources = {t["source"] for t in trades}

    assert any(s == "vanilla_vtp" for s in sources), "Missing vanilla VTP trades"
    assert any("moonlight" in t["type"] for t in trades), "Missing Moonlight trades"
    assert any("add" in s for s in sources), "Missing mod add trades"
    # Total: 1 vanilla + 1 moonlight + 1 mod add = 3
    assert len(trades) == 3
