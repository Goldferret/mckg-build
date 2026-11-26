"""
Tests for mkgraph.extract_recipes.

Covers:
  1. _get_result — all result field variants
  2. _extract_ingredients — shaped, shapeless, smelting, deduplication, generic fallback
  3. extract_recipes — dump-wins merge, source tagging, conditions_met preservation,
     ingredient and data survival
"""

import json
import pytest
from pathlib import Path
from mkgraph.extract_recipes import _get_result, _extract_ingredients, extract_recipes


# ---------------------------------------------------------------------------
# 1. _get_result
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("data,expected_item,expected_count", [
    ({"result": {"item": "demo:plate", "count": 3}},  "demo:plate", 3),
    ({"result": {"id":   "demo:plate", "count": 3}},  "demo:plate", 3),
    ({"result": "demo:plate"},                        "demo:plate", 1),
    ({"result": {"id":   "demo:plate"}},               "demo:plate", 1),
    ({},                                               None,         1),
    ({"result": None},                                 None,         1),
    # "item" wins over "id" when both are present
    ({"result": {"item": "demo:item_wins", "id": "demo:id_loses", "count": 2}},
     "demo:item_wins", 2),
])
def test_get_result(data, expected_item, expected_count):
    item, count = _get_result(data)
    assert item == expected_item
    assert count == expected_count


# ---------------------------------------------------------------------------
# 2. _extract_ingredients
# ---------------------------------------------------------------------------

def test_shaped_exact_ingredients():
    data = {
        "type": "minecraft:crafting_shaped",
        "key": {
            "X": {"item": "demo:ingot"},
            "#": {"tag": "c:rods/wooden"},
        },
    }
    ings = _extract_ingredients(data)
    assert ings == [
        {"kind": "item", "value": "demo:ingot"},
        {"kind": "tag",  "value": "c:rods/wooden"},
    ]


def test_shapeless_exact_ingredients():
    data = {
        "type": "minecraft:crafting_shapeless",
        "ingredients": [{"id": "demo:ingot"}, "#c:rods"],
    }
    ings = _extract_ingredients(data)
    assert ings == [
        {"kind": "item", "value": "demo:ingot"},
        {"kind": "tag",  "value": "c:rods"},
    ]


@pytest.mark.parametrize("rtype", [
    "minecraft:smelting",
    "minecraft:blasting",
    "minecraft:smoking",
    "minecraft:campfire_cooking",
])
def test_smelting_family(rtype):
    data = {"type": rtype, "ingredient": {"item": "demo:ore"}}
    ings = _extract_ingredients(data)
    assert ings == [{"kind": "item", "value": "demo:ore"}]


@pytest.mark.parametrize("field,payload", [
    ("ingredients", [{"item": "demo:mat"}]),
    ("ingredient",  {"item": "demo:mat"}),
])
def test_generic_fallback(field, payload):
    """Custom recipe types fall through to the generic field names."""
    data = {"type": "mekanism:crushing", field: payload}
    ings = _extract_ingredients(data)
    assert ings == [{"kind": "item", "value": "demo:mat"}]


def test_deduplicates_preserving_kind_distinction():
    data = {
        "type": "minecraft:crafting_shapeless",
        "ingredients": [
            [{"item": "demo:x"}, {"tag": "demo:x"}],
            {"item": "demo:x"},
            "#demo:x",
        ],
    }
    ings = _extract_ingredients(data)
    # Both kind=item and kind=tag for "demo:x" must appear, once each
    assert ings == [
        {"kind": "item", "value": "demo:x"},
        {"kind": "tag",  "value": "demo:x"},
    ]


# ---------------------------------------------------------------------------
# 3. extract_recipes — dump-wins merge
# ---------------------------------------------------------------------------

DUMP_INGREDIENTS = [{"item": "demo:dump_input"}]
JAR_RECIPE_DATA  = {
    "type": "minecraft:crafting_shaped",
    "key": {"X": {"item": "demo:jar_input"}},
    "result": {"id": "demo:jar_output"},
    # This mod is NOT in installed, so conditions_met=0 on the JAR record.
    "neoforge:conditions": [
        {"type": "neoforge:mod_loaded", "modid": "missing_dependency"}
    ],
}
DUMP_RECIPE_DATA = {
    "type": "mekanism:crushing",
    "ingredients": [{"item": "demo:dump_input"}],
    "result": {"item": "demo:dump_output", "count": 4},
}


def _write_dump(tmp_path: Path, recipes: list[dict]) -> Path:
    dump_dir = tmp_path / "dumps"
    dump_dir.mkdir()
    (dump_dir / "recipes_000.json").write_text(json.dumps(recipes), encoding="utf-8")
    return dump_dir


def test_dump_wins_over_jar(jar_factory, tmp_path):
    """
    When a JAR recipe's condition fails (conditions_met=0) and the dump
    has the same ID, the dump record must win on ALL fields including
    conditions_met, type, ingredients, and raw data.
    """
    jar = jar_factory("demo.jar", {
        "META-INF/neoforge.mods.toml": '[[mods]]\nmodId = "demo"\n',
        "data/demo/recipe/tools/plate.json": JAR_RECIPE_DATA,
    })

    dump_dir = _write_dump(tmp_path, [{
        "id":   "demo:tools/plate",
        "type": "mekanism:crushing",
        "mod":  "demo",
        "data": DUMP_RECIPE_DATA,
    }])

    # Confirm the JAR record has conditions_met=0 when extracted in isolation
    jar_only = extract_recipes([jar], installed={"demo"})
    assert len(jar_only) == 1
    assert jar_only[0]["conditions_met"] == 0

    # Merged result: dump wins
    records = extract_recipes([jar], installed={"demo"}, dump_dir=dump_dir)
    assert len(records) == 1   # exact cardinality, no silent duplicate collapse
    shared = records[0]
    assert shared["id"]            == "demo:tools/plate"
    assert shared["type"]          == "mekanism:crushing"
    assert shared["result_item"]   == "demo:dump_output"
    assert shared["result_count"]  == 4
    assert shared["conditions_met"] == 1   # dump is always authoritative
    assert shared["source"]        == "dump+jar"
    assert shared["ingredients"]   == [{"kind": "item", "value": "demo:dump_input"}]
    assert shared["data"]          == DUMP_RECIPE_DATA


def test_dump_only_and_jar_only_both_present(jar_factory, tmp_path):
    jar = jar_factory("demo.jar", {
        "data/demo/recipe/jar_only.json": {
            "type": "minecraft:crafting_shaped",
            "result": {"item": "demo:jar_item"},
        },
    })
    dump_dir = _write_dump(tmp_path, [{
        "id":   "demo:dump_only",
        "type": "minecraft:crafting_shapeless",
        "mod":  "demo",
        "data": {"type": "minecraft:crafting_shapeless", "result": {"item": "demo:dump_item"}},
    }])

    records = extract_recipes([jar], installed=set(), dump_dir=dump_dir)
    assert len(records) == 2    # exact cardinality before collapsing
    assert {r["id"] for r in records} == {"demo:dump_only", "demo:jar_only"}

    by_id = {r["id"]: r for r in records}
    assert by_id["demo:dump_only"]["source"] == "dump"
    assert by_id["demo:jar_only"]["source"]  == "jar"

# ---------------------------------------------------------------------------
# 3. _get_result — new fallback paths
# ---------------------------------------------------------------------------

from mkgraph.extract_recipes import _first_item_entry, _conditions_met, _unwrap_conditional


def test_get_result_result_as_list_thermal():
    """Thermal-style: result is an array with chance multipliers."""
    data = {
        "type": "thermal:insolator",
        "result": [
            {"item": "thermal:rubber", "chance": 2.0},
            {"item": "minecraft:slime_ball", "chance": 0.5},
        ],
    }
    item, count = _get_result(data)
    assert item == "thermal:rubber"
    assert count == 1  # no count field → 1


def test_get_result_result_as_list_farmersdelight():
    """FarmersDelight cutting: result is an array."""
    data = {
        "type": "farmersdelight:cutting",
        "result": [{"item": "minecraft:acacia_planks"}],
    }
    item, count = _get_result(data)
    assert item == "minecraft:acacia_planks"
    assert count == 1


def test_get_result_results_array_create():
    """Create-style: results array, first guaranteed output."""
    data = {
        "type": "create:crushing",
        "results": [
            {"item": "demo:dust", "count": 2},
            {"item": "demo:byproduct", "chance": 0.25},
        ],
    }
    item, count = _get_result(data)
    assert item == "demo:dust"
    assert count == 2


def test_get_result_results_array_fluid_only_skipped():
    """Create mixing where first entry is a fluid — skip to first item entry."""
    data = {
        "type": "create:mixing",
        "results": [
            {"fluid": "create:dye", "nbt": "{Color:\"black\"}", "railways:amount": 81000},
            {"item": "demo:pigment"},
        ],
    }
    item, count = _get_result(data)
    # Fluid-only entries skipped; next is the item entry
    assert item == "demo:pigment"


def test_get_result_item_outputs_mi():
    """Modern Industrialization: item_outputs with 'amount' as count key."""
    data = {
        "type": "modern_industrialization:macerator",
        "item_outputs": [{"item": "ad_astra:raw_calorite", "amount": 3}],
    }
    item, count = _get_result(data)
    assert item == "ad_astra:raw_calorite"
    assert count == 3


def test_get_result_output_botania():
    """Botania mana_infusion: singular 'output' dict."""
    data = {
        "type": "botania:mana_infusion",
        "output": {"item": "minecraft:acacia_leaves", "count": 2},
    }
    item, count = _get_result(data)
    assert item == "minecraft:acacia_leaves"
    assert count == 2


def test_get_result_dict_item_is_tag_returns_none():
    """Older Forge: result.item is a tag dict, not a string — should return None."""
    data = {"result": {"item": {"tag": "forge:ingots/iron"}, "count": 1}}
    item, count = _get_result(data)
    assert item is None
    assert count == 1


def test_get_result_tconstruct_melting_fluid_only():
    """tconstruct:melting produces a fluid — result_item should be 'fluid:<id>'."""
    data = {
        "type": "tconstruct:melting",
        "result": {"fluid": "tconstruct:blazing_blood", "amount": 100},
    }
    item, count = _get_result(data)
    assert item == "fluid:tconstruct:blazing_blood"
    assert count == 100


def test_get_result_tconstruct_casting_string():
    """tconstruct:casting_table with string result."""
    data = {
        "type": "tconstruct:casting_table",
        "result": "tconstruct:seared_reinforcement",
    }
    item, count = _get_result(data)
    assert item == "tconstruct:seared_reinforcement"
    assert count == 1


# ---------------------------------------------------------------------------
# 4. forge:conditional helpers
# ---------------------------------------------------------------------------

def test_conditions_met_mod_loaded_true():
    conds = [{"type": "forge:mod_loaded", "modid": "mymod"}]
    assert _conditions_met(conds, {"mymod", "othermod"}) is True


def test_conditions_met_mod_loaded_false():
    conds = [{"type": "forge:mod_loaded", "modid": "missingmod"}]
    assert _conditions_met(conds, {"mymod"}) is False


def test_conditions_met_forge_not():
    conds = [{"type": "forge:not", "value": {"type": "forge:mod_loaded", "modid": "conflictmod"}}]
    assert _conditions_met(conds, {"mymod"}) is True           # conflict not installed
    assert _conditions_met(conds, {"mymod", "conflictmod"}) is False  # conflict IS installed


def test_conditions_met_empty():
    assert _conditions_met([], {"anything"}) is True


def test_unwrap_conditional_picks_first_satisfied():
    data = {
        "type": "forge:conditional",
        "recipes": [
            {
                "conditions": [{"type": "forge:mod_loaded", "modid": "mod_a"}],
                "recipe": {"type": "minecraft:crafting_shaped", "result": {"item": "mod_a:widget"}},
            },
            {
                "conditions": [{"type": "forge:mod_loaded", "modid": "mod_b"}],
                "recipe": {"type": "minecraft:crafting_shaped", "result": {"item": "mod_b:widget"}},
            },
        ],
    }
    # Only mod_b installed → picks second entry
    inner = _unwrap_conditional(data, {"mod_b"})
    assert inner is not None
    assert inner["result"]["item"] == "mod_b:widget"


def test_unwrap_conditional_none_when_no_conditions_met():
    data = {
        "type": "forge:conditional",
        "recipes": [
            {
                "conditions": [{"type": "forge:mod_loaded", "modid": "absent_mod"}],
                "recipe": {"type": "minecraft:crafting_shaped", "result": {"item": "absent_mod:thing"}},
            },
        ],
    }
    assert _unwrap_conditional(data, {"mymod"}) is None

# ---------------------------------------------------------------------------
# 5. Fluid output tracking
# ---------------------------------------------------------------------------

def test_get_result_fluid_dict_result():
    """tconstruct:melting — result is a fluid dict → 'fluid:<id>'."""
    data = {
        "type": "tconstruct:melting",
        "result": {"fluid": "tconstruct:blazing_blood", "amount": 100},
    }
    item, count = _get_result(data)
    assert item == "fluid:tconstruct:blazing_blood"
    assert count == 100


def test_get_result_fluid_via_output_key():
    """Mod that uses 'output: {fluid, amount}'."""
    data = {"output": {"fluid": "mekanism:sulfuric_acid", "amount": 1000}}
    item, count = _get_result(data)
    assert item == "fluid:mekanism:sulfuric_acid"
    assert count == 1000


def test_first_item_entry_fluid_fallback():
    """When results list has only fluid entries, return fluid: prefix."""
    lst = [{"fluid": "create:dye", "amount": 81000}]
    item, count = _first_item_entry(lst)
    assert item == "fluid:create:dye"
    assert count == 81000


def test_first_item_entry_item_beats_fluid():
    """Item entries take priority over fluid entries even if fluid appears first."""
    lst = [
        {"fluid": "create:some_fluid", "amount": 500},
        {"item": "create:pigment_bag", "count": 1},
    ]
    item, count = _first_item_entry(lst)
    assert item == "create:pigment_bag"
    assert count == 1

# ---------------------------------------------------------------------------
# 6. Tag-output results (TiC casting, ore-dict unification)
# ---------------------------------------------------------------------------

def test_get_result_tag_output():
    """TiC casting_table with result: {tag: ...} → 'tag:<id>'."""
    data = {
        "type": "tconstruct:casting_table",
        "result": {"tag": "forge:plates/brick"},
    }
    item, count = _get_result(data)
    assert item == "tag:forge:plates/brick"
    assert count == 1


def test_get_result_item_wins_over_tag():
    """When both item and tag are present, item wins."""
    data = {"result": {"item": "demo:plate", "tag": "forge:plates", "count": 2}}
    item, count = _get_result(data)
    assert item == "demo:plate"
    assert count == 2


# ---------------------------------------------------------------------------
# 7. porting_lib:conditional (Fabric equivalent of forge:conditional)
# ---------------------------------------------------------------------------

def test_unwrap_porting_lib_conditional():
    """porting_lib:conditional uses same structure as forge:conditional."""
    data = {
        "type": "porting_lib:conditional",
        "recipes": [
            {
                "conditions": [{"type": "forge:mod_loaded", "modid": "mymod"}],
                "recipe": {"type": "minecraft:crafting_shaped", "result": {"item": "mymod:widget"}},
            },
        ],
    }
    inner = _unwrap_conditional(data, {"mymod"})
    assert inner is not None
    assert inner["result"]["item"] == "mymod:widget"
