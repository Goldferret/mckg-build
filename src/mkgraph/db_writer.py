"""
SurrealDB knowledge-graph writer (embedded SDK).

Uses the SurrealDB Python SDK with the embedded SurrealKV engine:

    with Surreal("surrealkv://./knowledge.db") as db:
        ...

No external database server or import step required. The output is a
SurrealKV directory (``<output>.db/``) that can be served with:

    surreal db start --path ./knowledge.db

Performance: writes are batched into ~500-statement inline transactions,
giving ~40k statements/second on a modern laptop (~8–10s for a large pack).
"""

from __future__ import annotations

import json
from pathlib import Path

from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn
from surrealdb import RecordID, Surreal

console = Console()

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_BATCH = 500  # statements per inline transaction


def _safe_id(raw_id: str) -> str:
    """Convert a namespaced ID to a SurrealDB-safe record ID string."""
    return (
        raw_id
        .replace(":", "__")
        .replace("/", "_")
        .replace("-", "_")
        .replace(".", "_")
        .replace(" ", "_")  # guard against malformed IDs with spaces
        .lstrip("#")        # strip any stray tag-shorthand prefix
    )


def _mod_from_id(item_id: str) -> str:
    """
    Derive the mod namespace from an item/fluid/tag prefixed ID.
      fluid:namespace:name  → namespace
      tag:namespace:name    → namespace
      namespace:name        → namespace
    """
    parts = item_id.split(":")
    if item_id.startswith(("fluid:", "tag:")) and len(parts) >= 3:
        return parts[1]
    return parts[0]


def _flush(db: Surreal, batch: list[str]) -> None:
    """Execute a batch of SurrealQL statements inside a single transaction.

    If the whole batch fails (e.g. one malformed statement triggers a
    transaction rollback), retries each statement individually so only the
    bad statement is skipped rather than the whole batch.
    """
    if not batch:
        return
    sql = "BEGIN TRANSACTION;\n" + "\n".join(batch) + "\nCOMMIT TRANSACTION;"
    try:
        db.query(sql).execute()
    except Exception:
        # Batch failed — retry per-statement to isolate the bad one
        for stmt in batch:
            try:
                db.query(stmt).execute()
            except Exception as e:
                console.print(f"  [yellow]⚠ Skipped statement ({e}): {stmt[:120]}[/yellow]")
    batch.clear()


def _add(batch: list[str], stmt: str, db: Surreal) -> None:
    """Append a statement to the batch; flush when the batch reaches _BATCH."""
    batch.append(stmt)
    if len(batch) >= _BATCH:
        _flush(db, batch)


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

_SCHEMA_SQL = """
-- Node tables
DEFINE TABLE item SCHEMAFULL;
DEFINE FIELD id_str ON item TYPE string;
DEFINE FIELD mod    ON item TYPE string;

DEFINE TABLE recipe SCHEMAFULL;
DEFINE FIELD id_str         ON recipe TYPE string;
DEFINE FIELD type           ON recipe TYPE string;
DEFINE FIELD mod            ON recipe TYPE string;
DEFINE FIELD result_item    ON recipe TYPE string;
DEFINE FIELD result_count   ON recipe TYPE int;
DEFINE FIELD source         ON recipe TYPE string;
DEFINE FIELD conditions_met ON recipe TYPE int;

DEFINE TABLE tag SCHEMAFULL;
DEFINE FIELD id_str    ON tag TYPE string;
DEFINE FIELD registry  ON tag TYPE string;
DEFINE FIELD members   ON tag TYPE array<string>;

DEFINE TABLE loot_source SCHEMAFULL;
DEFINE FIELD id_str     ON loot_source TYPE string;
DEFINE FIELD category   ON loot_source TYPE string;
DEFINE FIELD mod        ON loot_source TYPE string;
DEFINE FIELD source     ON loot_source TYPE string;
DEFINE FIELD drop_count ON loot_source TYPE int;

DEFINE TABLE trade SCHEMAFULL;
DEFINE FIELD profession   ON trade TYPE string;
DEFINE FIELD tier         ON trade TYPE string;
DEFINE FIELD type         ON trade TYPE string;
DEFINE FIELD price_item   ON trade TYPE string;
DEFINE FIELD price_count  ON trade TYPE int;
DEFINE FIELD offer_item   ON trade TYPE string;
DEFINE FIELD offer_count  ON trade TYPE int;
DEFINE FIELD max_uses     ON trade TYPE int;
DEFINE FIELD mod          ON trade TYPE string;
DEFINE FIELD source       ON trade TYPE string;

DEFINE TABLE ore_spawn SCHEMAFULL;
DEFINE FIELD ore_block       ON ore_spawn TYPE string;
DEFINE FIELD deepslate_block ON ore_spawn TYPE string;
DEFINE FIELD vein_size       ON ore_spawn TYPE int;
DEFINE FIELD discard_chance  ON ore_spawn TYPE float;
DEFINE FIELD min_y           ON ore_spawn TYPE int;
DEFINE FIELD max_y           ON ore_spawn TYPE int;
DEFINE FIELD biomes          ON ore_spawn TYPE array<string>;
DEFINE FIELD mod             ON ore_spawn TYPE string;
DEFINE FIELD source          ON ore_spawn TYPE string;

-- Edge tables
DEFINE TABLE PRODUCED_BY  SCHEMALESS TYPE RELATION;
DEFINE TABLE REQUIRES     SCHEMALESS TYPE RELATION;
DEFINE TABLE REQUIRES_TAG SCHEMALESS TYPE RELATION;
DEFINE TABLE TAGGED_AS    SCHEMALESS TYPE RELATION;
DEFINE TABLE TRADED_FOR   SCHEMALESS TYPE RELATION;
DEFINE TABLE DROPS_FROM   SCHEMAFULL TYPE RELATION;
DEFINE FIELD min_count  ON DROPS_FROM TYPE int;
DEFINE FIELD max_count  ON DROPS_FROM TYPE int;
DEFINE FIELD conditions ON DROPS_FROM TYPE array<string>;
"""

# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def write_graph(
    output: Path,
    recipes: list[dict],
    tags_resolved: dict,         # (registry, full_id) → set[str]
    loot: list[dict],
    worldgen: list[dict],
    trades: list[dict],
) -> None:
    """
    Write the full knowledge graph to an embedded SurrealKV database.

    The database is written to ``<output>.db/`` and can be served with:

        surreal db start --path <output>.db
    """
    db_path = output.with_suffix(".db")

    # Remove stale database directory if it exists
    if db_path.exists():
        import shutil
        shutil.rmtree(db_path)

    console.print(f"[bold]Writing knowledge graph to {db_path}[/bold]")

    with Surreal(f"surrealkv://{db_path}") as db:
        db.use("mc", "pack")

        # --- Schema -------------------------------------------------------
        console.print("  Defining schema...")
        db.query(_SCHEMA_SQL).execute()

        batch: list[str] = []

        # --- Collect all item IDs -----------------------------------------
        console.print("  Collecting item IDs...")
        item_ids: set[str] = set()

        for rec in recipes:
            result_item_val = rec.get("result_item")
            if isinstance(result_item_val, str) and result_item_val and not result_item_val.startswith("#"):
                item_ids.add(result_item_val)
            for ing in rec.get("ingredients", []):
                if ing["kind"] == "item" and not ing["value"].startswith("#"):
                    item_ids.add(ing["value"])

        for (registry, full_id), members in tags_resolved.items():
            if registry == "item":
                for m in members:
                    if not m.startswith("#"):
                        item_ids.add(m)

        for rec in loot:
            for drop in rec.get("drops", []):
                item = drop.get("item", "")
                if item and not item.startswith("#"):
                    item_ids.add(item)

        for rec in trades:
            for field in ("price_item", "offer_item"):
                v = rec.get(field)
                if v and not v.startswith("#"):
                    item_ids.add(v)

        # --- Item nodes ---------------------------------------------------
        console.print(f"  Writing {len(item_ids)} item nodes...")
        for item_id in sorted(item_ids):
            sid = _safe_id(item_id)
            mod = _mod_from_id(item_id)
            _add(batch, f"UPSERT item:{sid} SET id_str = {json.dumps(item_id)}, mod = {json.dumps(mod)};", db)
        _flush(db, batch)

        # --- Collect tag IDs (resolved + stubs for unresolved refs) -------
        tag_ids: set[tuple[str, str]] = set(tags_resolved.keys())
        for rec in recipes:
            for ing in rec.get("ingredients", []):
                if ing["kind"] == "tag":
                    tag_ids.add(("item", ing["value"]))

        # --- Tag nodes ----------------------------------------------------
        console.print(f"  Writing {len(tag_ids)} tag nodes...")
        for (registry, full_id) in sorted(tag_ids):
            sid = _safe_id(full_id)
            members = sorted(tags_resolved.get((registry, full_id), []))
            _add(
                batch,
                f"UPSERT tag:{registry}__{sid} SET "
                f"id_str = {json.dumps(full_id)}, "
                f"registry = {json.dumps(registry)}, "
                f"members = {json.dumps(members)};",
                db,
            )
        _flush(db, batch)

        # --- TAGGED_AS edges ----------------------------------------------
        console.print("  Writing TAGGED_AS edges...")
        for (registry, full_id), members in tags_resolved.items():
            if registry != "item":
                continue
            tag_sid = _safe_id(full_id)
            for member in members:
                if member.startswith("#"):
                    continue
                item_sid = _safe_id(member)
                _add(batch, f"RELATE item:{item_sid}->TAGGED_AS->tag:item__{tag_sid};", db)
        _flush(db, batch)

        # --- Recipe nodes + edges -----------------------------------------
        console.print(f"  Writing {len(recipes)} recipe nodes...")
        for rec in recipes:
            rid = rec["id"]
            rsid = _safe_id(rid)
            result_item = rec.get("result_item") or ""
            _add(
                batch,
                f"UPSERT recipe:{rsid} SET "
                f"id_str = {json.dumps(rid)}, "
                f"type = {json.dumps(rec.get('type', ''))}, "
                f"mod = {json.dumps(rec.get('mod', ''))}, "
                f"result_item = {json.dumps(result_item)}, "
                f"result_count = {rec.get('result_count', 1)}, "
                f"source = {json.dumps(rec.get('source', ''))}, "
                f"conditions_met = {rec.get('conditions_met', 1)};",
                db,
            )
        _flush(db, batch)

        console.print("  Writing PRODUCED_BY / REQUIRES / REQUIRES_TAG edges...")
        for rec in recipes:
            rsid = _safe_id(rec["id"])
            result_item = rec.get("result_item") or ""
            if result_item and not result_item.startswith("#"):
                isid = _safe_id(result_item)
                _add(batch, f"RELATE item:{isid}->PRODUCED_BY->recipe:{rsid};", db)
            for ing in rec.get("ingredients", []):
                if ing["kind"] == "item":
                    isid = _safe_id(ing["value"])
                    _add(batch, f"RELATE recipe:{rsid}->REQUIRES->item:{isid};", db)
                else:
                    tsid = _safe_id(ing["value"])
                    _add(batch, f"RELATE recipe:{rsid}->REQUIRES_TAG->tag:item__{tsid};", db)
        _flush(db, batch)

        # --- Loot source nodes + DROPS_FROM edges -------------------------
        console.print(f"  Writing {len(loot)} loot source nodes...")
        for rec in loot:
            lid = _safe_id(rec["id"])
            _add(
                batch,
                f"UPSERT loot_source:{lid} SET "
                f"id_str = {json.dumps(rec['id'])}, "
                f"category = {json.dumps(rec.get('category', ''))}, "
                f"mod = {json.dumps(rec.get('mod', ''))}, "
                f"source = {json.dumps(rec.get('source', ''))}, "
                f"drop_count = {len(rec.get('drops', []))};",
                db,
            )
        _flush(db, batch)

        for rec in loot:
            lid = _safe_id(rec["id"])
            for drop in rec.get("drops", []):
                item = drop.get("item", "")
                if not item or item.startswith("#"):
                    continue
                isid = _safe_id(item)
                conds = json.dumps(drop.get("conditions", []))
                _add(
                    batch,
                    f"RELATE item:{isid}->DROPS_FROM->loot_source:{lid} "
                    f"SET min_count = {int(drop.get('min_count') or 1)}, "
                    f"max_count = {int(drop.get('max_count') or 1)}, "
                    f"conditions = {conds};",
                    db,
                )
        _flush(db, batch)

        # --- Ore spawn nodes ----------------------------------------------
        console.print(f"  Writing {len(worldgen)} ore spawn nodes...")
        for rec in worldgen:
            oid = _safe_id(rec["id"])
            _add(
                batch,
                f"UPSERT ore_spawn:{oid} SET "
                f"ore_block = {json.dumps(rec.get('ore_block', ''))}, "
                f"deepslate_block = {json.dumps(rec.get('deepslate_block') or '')}, "
                f"vein_size = {int(rec.get('vein_size') or 0)}, "
                f"discard_chance = {float(rec.get('discard_chance') or 0.0)}, "
                f"min_y = {int(rec.get('min_y') or -64)}, "
                f"max_y = {int(rec.get('max_y') or 320)}, "
                f"biomes = {json.dumps(rec.get('biomes', []))}, "
                f"mod = {json.dumps(rec.get('mod', ''))}, "
                f"source = {json.dumps(rec.get('source', ''))};",
                db,
            )
        _flush(db, batch)

        # --- Trade nodes + TRADED_FOR edges -------------------------------
        console.print(f"  Writing {len(trades)} trade nodes...")
        for i, rec in enumerate(trades):
            tid = f"trade_{i}"
            _add(
                batch,
                f"UPSERT trade:{tid} SET "
                f"profession = {json.dumps(rec.get('profession', ''))}, "
                f"tier = {json.dumps(rec.get('tier', ''))}, "
                f"type = {json.dumps(rec.get('type', ''))}, "
                f"price_item = {json.dumps(rec.get('price_item', '') or '')}, "
                f"price_count = {int(rec.get('price_count') or 1)}, "
                f"offer_item = {json.dumps(rec.get('offer_item', '') or '')}, "
                f"offer_count = {int(rec.get('offer_count') or 1)}, "
                f"max_uses = {int(rec.get('max_uses') or 12)}, "
                f"mod = {json.dumps(rec.get('mod', ''))}, "
                f"source = {json.dumps(rec.get('source', ''))};",
                db,
            )
        _flush(db, batch)

        for i, rec in enumerate(trades):
            offer_item = rec.get("offer_item") or ""
            if offer_item and not offer_item.startswith("#"):
                isid = _safe_id(offer_item)
                _add(batch, f"RELATE item:{isid}->TRADED_FOR->trade:trade_{i};", db)
        _flush(db, batch)

        # --- Summary ------------------------------------------------------
        counts = db.query(
            "SELECT count() FROM item GROUP ALL;"
            "SELECT count() FROM recipe GROUP ALL;"
            "SELECT count() FROM tag GROUP ALL;"
            "SELECT count() FROM loot_source GROUP ALL;"
            "SELECT count() FROM ore_spawn GROUP ALL;"
            "SELECT count() FROM trade GROUP ALL;"
        ).execute()

        n_item    = counts[0][0]["count"] if counts[0] else 0
        n_recipe  = counts[1][0]["count"] if counts[1] else 0
        n_tag     = counts[2][0]["count"] if counts[2] else 0
        n_loot    = counts[3][0]["count"] if counts[3] else 0
        n_ore     = counts[4][0]["count"] if counts[4] else 0
        n_trade   = counts[5][0]["count"] if counts[5] else 0

    console.print(f"[green]✓ Knowledge graph written to {db_path}[/green]")
    console.print(f"  Items:        {n_item}")
    console.print(f"  Recipes:      {n_recipe}")
    console.print(f"  Tags:         {n_tag}")
    console.print(f"  Loot sources: {n_loot}")
    console.print(f"  Ore spawns:   {n_ore}")
    console.print(f"  Trades:       {n_trade}")
