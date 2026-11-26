"""
Tests for mkgraph.jar_utils.

Covers:
  1. iter_data_files — file enumeration, singular/plural dirs, multi-namespace,
     bad-zip and malformed-JSON resilience
  2. evaluate_conditions — all pass/fail/unknown combinations including
     precedence of definite-fail over unknown
  3. build_installed_modids — toml metadata reading, multi-mod JARs,
     fallback to data-namespace inference
"""

import pytest
from mkgraph.jar_utils import (
    iter_data_files,
    evaluate_conditions,
    build_installed_modids,
    RECIPE_DIRS,
)


# ---------------------------------------------------------------------------
# 1. iter_data_files
# ---------------------------------------------------------------------------

def test_enumerates_both_recipe_dir_spellings(jar_factory):
    jar = jar_factory("demo.jar", {
        "data/demo/recipe/tools/hammer.json":  {"result": "demo:hammer"},
        "data/demo/recipes/plate.json":        {"result": "demo:plate"},
        "data/other/recipe/gear.json":         {"result": "other:gear"},
        # should NOT be yielded:
        "data/demo/tags/item/tools.json":      {"values": []},
        "assets/demo/recipe/not_data.json":    {},
        "data/demo/recipe/readme.txt":         "not json",
        "data/demo.json":                      {},
    })

    rows = list(iter_data_files(jar, {"recipe", "recipes"}))
    # Assert exact cardinality before collapsing
    assert len(rows) == 3

    by_path = {path: (modid, data) for modid, path, data in rows}
    assert by_path == {
        "data/demo/recipe/tools/hammer.json": ("demo", {"result": "demo:hammer"}),
        "data/demo/recipes/plate.json":       ("demo", {"result": "demo:plate"}),
        "data/other/recipe/gear.json":        ("other", {"result": "other:gear"}),
    }


def test_resilience_malformed_beside_valid(jar_factory):
    """Malformed JSON is skipped; the valid record is returned with correct identity."""
    jar = jar_factory("test.jar", {
        "data/demo/recipe/good.json": {"result": "demo:good"},
        "data/demo/recipe/bad.json":  "{not valid json",
    })
    rows = list(iter_data_files(jar, RECIPE_DIRS))
    assert rows == [("demo", "data/demo/recipe/good.json", {"result": "demo:good"})]


def test_resilience_empty_archive(jar_factory):
    jar = jar_factory("empty.jar", {})
    assert list(iter_data_files(jar, RECIPE_DIRS)) == []


def test_bad_zip_file(tmp_path):
    """A file that is not a valid ZIP is silently skipped."""
    bad = tmp_path / "bad.jar"
    bad.write_bytes(b"not a zip file")
    assert list(iter_data_files(bad, RECIPE_DIRS)) == []


# ---------------------------------------------------------------------------
# 2. evaluate_conditions
# ---------------------------------------------------------------------------

INSTALLED = {"demo", "mod_a"}


@pytest.mark.parametrize("conditions,expected", [
    # no conditions field
    ({}, 1),
    # empty list
    ({"neoforge:conditions": []}, 1),
    # installed mod → pass
    ({"neoforge:conditions": [{"type": "neoforge:mod_loaded", "modid": "demo"}]}, 1),
    # missing mod → fail
    ({"neoforge:conditions": [{"type": "neoforge:mod_loaded", "modid": "missing"}]}, 0),
    # explicit true
    ({"neoforge:conditions": [{"type": "neoforge:true"}]}, 1),
    # explicit false
    ({"neoforge:conditions": [{"type": "neoforge:false"}]}, 0),
    # unknown type alone → unevaluated
    ({"neoforge:conditions": [{"type": "custom:unknown"}]}, 2),
    # unknown + passing → unevaluated (not all knowable)
    ({"neoforge:conditions": [
        {"type": "custom:unknown"},
        {"type": "neoforge:mod_loaded", "modid": "demo"},
    ]}, 2),
    # unknown + failing (unknown first) → fail wins
    ({"neoforge:conditions": [
        {"type": "custom:unknown"},
        {"type": "neoforge:mod_loaded", "modid": "missing"},
    ]}, 0),
    # unknown + failing (fail first) → fail wins regardless of order
    ({"neoforge:conditions": [
        {"type": "neoforge:mod_loaded", "modid": "missing"},
        {"type": "custom:unknown"},
    ]}, 0),
])
def test_evaluate_conditions(conditions, expected):
    assert evaluate_conditions(conditions, INSTALLED) == expected


# ---------------------------------------------------------------------------
# 3. build_installed_modids
# ---------------------------------------------------------------------------

NEOFORGE_TOML = """
[[mods]]
modId = "alpha"
[[mods]]
modId = "shared"
"""

LEGACY_TOML = """
[[mods]]
modId = "beta"
[[mods]]
modId = "shared"
"""


def test_reads_neoforge_mods_toml(jar_factory, tmp_path):
    a = jar_factory("a.jar", {"META-INF/neoforge.mods.toml": NEOFORGE_TOML})
    b = jar_factory("b.jar", {"META-INF/mods.toml": LEGACY_TOML})
    # Non-JAR file should be ignored
    (tmp_path / "not_a_jar.txt").write_text("modId = 'fake'")

    ids = build_installed_modids(tmp_path)
    assert ids == {"alpha", "beta", "shared"}


def test_empty_mods_dir(tmp_path):
    assert build_installed_modids(tmp_path) == set()


def test_fallback_to_data_namespace(jar_factory, tmp_path):
    """When no TOML is present, modids are inferred from data/ namespaces."""
    jar_factory("x.jar", {
        "data/xmod/recipe/thing.json": {"result": "xmod:thing"},
    })
    ids = build_installed_modids(tmp_path)
    assert "xmod" in ids
