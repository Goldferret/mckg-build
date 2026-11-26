"""
Tests for mkgraph.extract_tags.

Covers:
  1. resolve_tags — merging, deduplication, replace semantics, registry isolation
  2. resolve_nested — chain expansion, unknown refs kept, cycles terminate,
     registry boundaries respected
"""

import json
import pytest
from mkgraph.extract_tags import RawTag, resolve_tags, resolve_nested, extract_tags


def _rt(registry, full_id, replace, values, source="test"):
    ns, name = full_id.split(":", 1)
    return RawTag(registry=registry, namespace=ns, name=name,
                  replace=replace, values=values, source=source)


# ---------------------------------------------------------------------------
# 1. resolve_tags
# ---------------------------------------------------------------------------

def test_merge_additive_and_deduplicates():
    raw = [
        _rt("item", "c:ingots/iron", False, ["a:iron", "minecraft:iron_ingot"]),
        _rt("item", "c:ingots/iron", False, ["b:iron", "minecraft:iron_ingot"]),
    ]
    merged = resolve_tags(raw)
    assert merged[("item", "c:ingots/iron")] == {"a:iron", "b:iron", "minecraft:iron_ingot"}


def test_registries_are_isolated():
    raw = [
        _rt("item",  "c:ingots/iron", False, ["minecraft:iron_ingot"]),
        _rt("block", "c:ingots/iron", False, ["demo:iron_block"]),
    ]
    merged = resolve_tags(raw)
    assert merged[("item",  "c:ingots/iron")] == {"minecraft:iron_ingot"}
    assert merged[("block", "c:ingots/iron")] == {"demo:iron_block"}


def test_replace_discards_prior_values():
    raw = [
        _rt("item", "demo:tag", False, ["demo:old", "#demo:old_nested"]),
        _rt("item", "demo:tag", True,  ["demo:new"]),
        _rt("item", "demo:tag", False, ["demo:later"]),
    ]
    merged = resolve_tags(raw)
    assert merged[("item", "demo:tag")] == {"demo:new", "demo:later"}
    # The old nested reference must be gone — not just the concrete value.
    assert "#demo:old_nested" not in merged[("item", "demo:tag")]


def test_replace_to_empty_then_no_addition():
    raw = [
        _rt("item", "demo:tag", False, ["demo:old"]),
        _rt("item", "demo:tag", True,  []),
    ]
    merged = resolve_tags(raw)
    assert merged[("item", "demo:tag")] == set()


# ---------------------------------------------------------------------------
# 2. resolve_nested
# ---------------------------------------------------------------------------

def _fresh_chain():
    """a → b → c → item (independent dict copy each call)."""
    return {
        ("item", "demo:a"): {"#demo:b"},
        ("item", "demo:b"): {"#demo:c"},
        ("item", "demo:c"): {"demo:ingot"},
    }


def test_resolves_chain_fully():
    m = _fresh_chain()
    resolve_nested(m)
    assert m[("item", "demo:a")] == {"demo:ingot"}
    assert m[("item", "demo:b")] == {"demo:ingot"}
    assert m[("item", "demo:c")] == {"demo:ingot"}


def test_unknown_ref_is_kept():
    m = {("item", "demo:tag"): {"#demo:missing"}}
    resolve_nested(m)
    assert m[("item", "demo:tag")] == {"#demo:missing"}


def test_registry_boundary_not_crossed():
    m = {
        ("item",  "demo:a"): {"#demo:c"},
        ("item",  "demo:c"): {"demo:ingot"},
        ("block", "demo:c"): {"demo:block"},
    }
    resolve_nested(m)
    assert m[("item",  "demo:a")] == {"demo:ingot"}
    assert "demo:block" not in m[("item", "demo:a")]


def test_self_cycle_terminates():
    m = {("item", "demo:a"): {"#demo:a"}}
    resolve_nested(m, max_depth=10)
    # Must terminate; self-reference kept as-is
    assert m[("item", "demo:a")] == {"#demo:a"}


def test_max_depth_zero_leaves_unchanged():
    m = _fresh_chain()
    resolve_nested(m, max_depth=0)
    assert "#demo:b" in m[("item", "demo:a")]


def test_mutates_and_returns_same_object():
    m = _fresh_chain()
    result = resolve_nested(m)
    assert result is m


# ---------------------------------------------------------------------------
# 5. End-to-end ingestion: JAR + vanilla dir → extract_tags → resolve
# ---------------------------------------------------------------------------

def test_extract_tags_full_pipeline(jar_factory, tmp_path):
    """
    Vanilla ships a tag, mod A replaces it, mod B adds to it.
    The resolved map must contain only the mod members.
    """
    # Vanilla data directory
    vanilla = tmp_path / "vanilla"
    tag_path = vanilla / "data" / "minecraft" / "tags" / "item" / "tools.json"
    tag_path.parent.mkdir(parents=True, exist_ok=True)
    tag_path.write_text(json.dumps({"values": ["minecraft:old_tool"]}), encoding="utf-8")

    # Mod A: replace vanilla
    jar_a = jar_factory("mod_a.jar", {
        "data/minecraft/tags/item/tools.json": {
            "replace": True,
            "values": ["demo:new_tool"],
        }
    })

    # Mod B: add to the tag
    jar_b = jar_factory("mod_b.jar", {
        "data/minecraft/tags/item/tools.json": {
            "replace": False,
            "values": ["demo:later_tool"],
        }
    })

    raw = extract_tags([jar_a, jar_b], vanilla)
    merged = resolve_tags(raw)
    resolved = resolve_nested(merged)

    key = ("item", "minecraft:tools")
    assert key in resolved
    assert resolved[key] == {"demo:new_tool", "demo:later_tool"}
    assert "minecraft:old_tool" not in resolved[key]


def test_resolve_nested_depth1_partial_chain():
    """max_depth=1 resolves one hop, leaving deeper refs unresolved."""
    m = {
        ("item", "demo:a"): {"#demo:b"},
        ("item", "demo:b"): {"#demo:c"},
        ("item", "demo:c"): {"demo:ingot"},
    }
    resolve_nested(m, max_depth=1)
    # After 1 pass: a→b resolves to b's current value ({"#demo:c"})
    assert "#demo:c" in m[("item", "demo:a")]
    assert "demo:ingot" not in m[("item", "demo:a")]
