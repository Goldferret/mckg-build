"""
Tests for mkgraph.extract_loot.

Covers:
  1. _extract_drops — direct items, counts (all variants including nested bounds),
     container traversal, tag references, unsupported entries skipped
  2. ID parity — JAR reader and vanilla-directory reader produce identical IDs
     for the same loot table
"""

import json
import zipfile
from collections import Counter
import pytest
from pathlib import Path
from mkgraph.extract_loot import _extract_drops, _iter_loot_files


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _loot(functions=None):
    """Build a minimal loot table with one pool, one item entry."""
    entry = {"type": "minecraft:item", "name": "demo:gem"}
    if functions:
        entry["functions"] = functions
    return {"pools": [{"entries": [entry]}]}


def _set_count(count_val):
    return {"function": "minecraft:set_count", "count": count_val}


# ---------------------------------------------------------------------------
# 1. _extract_drops
# ---------------------------------------------------------------------------

def test_direct_item_defaults():
    table = {"pools": [{"entries": [
        {"type": "minecraft:item", "name": "demo:gem",
         "conditions": [{"condition": "minecraft:killed_by_player"}]},
    ]}]}
    drops = _extract_drops(table)
    assert drops == [{
        "item": "demo:gem", "min_count": 1, "max_count": 1,
        "conditions": ["minecraft:killed_by_player"],
    }]


def test_empty_table_produces_no_drops():
    assert _extract_drops({}) == []
    assert _extract_drops({"pools": []}) == []


def test_nameless_item_skipped():
    table = {"pools": [{"entries": [{"type": "minecraft:item"}]}]}
    assert _extract_drops(table) == []


@pytest.mark.parametrize("count_val,expected_min,expected_max", [
    # integer
    (3, 3, 3),
    # flat range dict
    ({"min": 2, "max": 5}, 2, 5),
    # typed range dict (uniform)
    ({"type": "minecraft:uniform", "min": 2, "max": 5}, 2, 5),
    # nested absolute bounds — the critical regression
    ({"type": "minecraft:uniform",
      "min": {"absolute": 2}, "max": {"absolute": 5}}, 2, 5),
    # mixed: nested min, flat max
    ({"min": {"absolute": 0}, "max": 5}, 0, 5),
])
def test_count_normalization(count_val, expected_min, expected_max):
    drops = _extract_drops(_loot(functions=[_set_count(count_val)]))
    assert len(drops) == 1
    assert drops[0]["min_count"] == expected_min
    assert drops[0]["max_count"] == expected_max
    assert type(drops[0]["min_count"]) is int
    assert type(drops[0]["max_count"]) is int


@pytest.mark.parametrize("container_type", [
    "minecraft:alternatives",
    "minecraft:group",
    "minecraft:sequence",
])
def test_container_traversal(container_type):
    table = {"pools": [{"entries": [{
        "type": container_type,
        "children": [
            {"type": "minecraft:item", "name": "demo:item_a"},
            {"type": container_type, "children": [
                {"type": "minecraft:item", "name": "demo:item_b"},
            ]},
        ],
    }]}]}
    drops = _extract_drops(table)
    # Exact cardinality: each leaf visited exactly once
    assert Counter(d["item"] for d in drops) == Counter({
        "demo:item_a": 1,
        "demo:item_b": 1,
    })


def test_multiple_pools_all_traversed():
    table = {"pools": [
        {"entries": [{"type": "minecraft:item", "name": "demo:pool1"}]},
        {"entries": [{"type": "minecraft:item", "name": "demo:pool2"}]},
    ]}
    drops = _extract_drops(table)
    assert Counter(d["item"] for d in drops) == Counter({
        "demo:pool1": 1,
        "demo:pool2": 1,
    })


def test_tag_entry_recorded_as_hash_ref():
    table = {"pools": [{"entries": [
        {"type": "minecraft:tag", "name": "c:gems"},
        {"type": "minecraft:empty"},
        {"type": "custom:unsupported"},
    ]}]}
    drops = _extract_drops(table)
    assert drops == [{"item": "#c:gems", "min_count": 1, "max_count": 1, "conditions": []}]


# ---------------------------------------------------------------------------
# 2. ID parity — JAR reader vs directory reader
# ---------------------------------------------------------------------------

LOOT_CONTENT = {"pools": [{"entries": [{"type": "minecraft:item", "name": "demo:drop"}]}]}
EXPECTED_DROPS = [{"item": "demo:drop", "min_count": 1, "max_count": 1, "conditions": []}]


def test_jar_and_dir_produce_same_loot_id(jar_factory, tmp_path):
    """
    Both readers must produce the same canonical ID so the deduplication
    merge in extract_loot works correctly.
    Canonical form: 'namespace:category/name', e.g. 'demo:entities/test'.
    """
    # --- JAR form ---
    jar = jar_factory("demo.jar", {
        "data/demo/loot_table/entities/test.json": LOOT_CONTENT,
    })
    jar_records = list(_iter_loot_files(jar, "jar"))

    # --- Directory form ---
    loot_dir = tmp_path / "vanilla" / "data" / "demo" / "loot_table" / "entities"
    loot_dir.mkdir(parents=True, exist_ok=True)
    (loot_dir / "test.json").write_text(json.dumps(LOOT_CONTENT), encoding="utf-8")
    dir_records = list(_iter_loot_files(tmp_path / "vanilla", "dir"))

    # Exact cardinality
    assert len(jar_records) == 1, f"JAR produced {len(jar_records)} records, expected 1"
    assert len(dir_records) == 1, f"DIR produced {len(dir_records)} records, expected 1"

    # Exact canonical ID
    for rec in [jar_records[0], dir_records[0]]:
        assert rec["id"]       == "demo:entities/test", f"Wrong ID: {rec['id']!r}"
        assert rec["category"] == "mob"
        assert rec["mod"]      == "demo"
        assert rec["drops"]    == EXPECTED_DROPS
