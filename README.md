# mckg-build

Build a [SurrealDB](https://surrealdb.com) knowledge graph from a Minecraft modpack. Designed as a data layer for LLM agents that reason about crafting, progression, and resource acquisition — a "JEI for agents."

> [!NOTE]
> **Heads-up:** this project was primarily AI-authored. The extractors, schema, tests, and this README were written by AI coding agents in interactive sessions, with a human operator directing the work, reviewing output, and validating results against real modpacks. Read the code before trusting it in production.

```bash
pip install mckg-build
```

## Support scope

This tool was **built and tested for modern NeoForge and Fabric packs (MC 1.16.5+)**. It is not a general-purpose tool for every Minecraft version.

### Tested packs

| Pack | MC Version | Loader | Recipes extracted |
|------|-----------|--------|-------------------|
| All The Mods 10 *(primary target)* | 1.21.1 | NeoForge | 108,505 (with KubeJS dump) |
| All The Mods 11 | 26.1.2 | NeoForge | 46,864 |
| Create: Above and Beyond | 1.16.5 | Forge | 10,037 |
| Prominence II | 1.20.1 | Fabric | 31,045 |

### Known limitations

- **Pre-1.13 packs produce an empty graph.** Packs like FTB Infinity (1.7.10) or FTB Ultimate (1.6.4) predate Minecraft's datapack system entirely — recipes are defined in Java code, not JSON files. The build completes without crashing but extracts nothing.
- **KubeJS dump requires KubeJS 6+ (MC 1.18.2+).** The bundled `mckg dump-setup` script uses the KubeJS 6 event API. Packs running KubeJS 1605.x (MC 1.16.5) will produce only a pre-script snapshot, not the final post-KubeJS recipe state.
- **Some modded machine types remain unresolved.** The build reports which recipe types had `result_item=None` (see Null result reporting). Those recipe nodes exist in the graph without a `PRODUCED_BY` edge.
- **Build time scales with pack size.** ATM10 (468 mods, 108k recipes) takes ~8 minutes on a modern laptop.

## What it extracts

| Data | Source |
|------|--------|
| Recipes | KubeJS dump (authoritative) + static JAR scan |
| Item/block tags | JAR `tags/` + vanilla mcmeta |
| Loot tables | JAR `loot_table/` + vanilla mcmeta |
| Ore worldgen | JAR `worldgen/` + vanilla mcmeta |
| Villager trades | VillagersTradingPlus + Moonlight mod trades |

**Recipe coverage:** vanilla, NeoForge, Fabric, and most common modded types (Create, Thermal, Modern Industrialization, TiC, Botania, FarmersDelight, and more). Fluid-producing recipes are tracked with a `fluid:` prefix; tag-output recipes with `tag:`.

## Usage

### Basic — point at a server directory

```bash
mckg build --server /path/to/server --output ./knowledge
```

`--server` expects a directory containing a `mods/` folder. It auto-detects the Minecraft version, loader (NeoForge/Fabric), vanilla data, and KubeJS dump if present. The output is a SurrealKV directory at `<output>.db/`.

### With a KubeJS dump (recommended for complete recipe coverage)

For packs with KubeJS, run the dump setup once before building:

```bash
# Install the dump script into your server
mckg dump-setup --server /path/to/server

# Start the server, let it fully load, then stop it.
# The dump is written to kubejs/dumps/ automatically.

# Now build — the dump is auto-detected
mckg build --server /path/to/server --output ./knowledge
```

### Explicit flags

```bash
mckg build \
  --mods   ./mods/ \
  --dump   ./kubejs/dumps/ \
  --output ./knowledge \
  --mc-version 1.21.1 \
  --loader neoforge
```

## Using the graph

### Option A — start a SurrealDB server

```bash
surreal db start --path ./knowledge.db
```

Then query with any SurrealDB client:

```bash
surreal db sql --endpoint http://localhost:8000 \
               --username root --password root \
               --namespace mc --database pack
```

### Option B — query directly via the Python SDK (no server needed)

```python
from surrealdb import Surreal
from collections import defaultdict

with Surreal("surrealkv://./knowledge.db") as db:
    db.use("mc", "pack")
    result = db.query("""
        SELECT ->PRODUCED_BY->recipe.{id_str, type, result_count} AS recipes
        FROM item WHERE id_str = 'minecraft:iron_ingot';
    """).execute()
    print(result[0])
```

## Graph schema

### Nodes

| Table | Key fields |
|-------|-----------|
| `item` | `id_str`, `mod` |
| `recipe` | `id_str`, `type`, `mod`, `result_item`, `result_count`, `source`, `conditions_met` |
| `tag` | `id_str`, `registry`, `members[]` |
| `loot_source` | `id_str`, `category`, `mod`, `drop_count` |
| `trade` | `profession`, `tier`, `price_item`, `offer_item` |
| `ore_spawn` | `ore_block`, `min_y`, `max_y`, `vein_size`, `biomes[]` |

### Edges

| Edge | Meaning |
|------|---------|
| `item -[PRODUCED_BY]-> recipe` | This item is output of this recipe |
| `recipe -[REQUIRES]-> item` | This recipe needs this item as input |
| `recipe -[REQUIRES_TAG]-> tag` | This recipe accepts any item in this tag |
| `item -[TAGGED_AS]-> tag` | This item belongs to this tag |
| `item -[DROPS_FROM]-> loot_source` | This item drops from this mob/chest |
| `item -[TRADED_FOR]-> trade` | This item is obtainable from a villager |

### Special item ID prefixes

- `fluid:namespace:name` — fluid-producing recipes (e.g. smeltery outputs)
- `tag:namespace:name` — tag-output recipes (e.g. TiC casting with ore-dict results)

### Record ID format

Namespaced IDs are encoded for SurrealDB by replacing `:` with `__` and `/` with `_`:

- `minecraft:iron_ingot` → `item:minecraft__iron_ingot`
- `forge:ingots/iron` (item tag) → `tag:item__forge__ingots_iron`

## Example queries

```surql
-- All ways to produce iron ingots (grouped by recipe type)
SELECT ->PRODUCED_BY->recipe.{id_str, type, result_count} AS recipes
FROM item WHERE id_str = 'minecraft:iron_ingot';

-- What ingredients does a diamond pickaxe recipe need?
SELECT ->REQUIRES->item.id_str AS items,
       ->REQUIRES_TAG->tag.id_str AS tags
FROM recipe WHERE id_str = 'minecraft:diamond_pickaxe';

-- What items drop from zombies?
SELECT in.id_str AS item, min_count, max_count
FROM DROPS_FROM WHERE out.id_str = 'minecraft:entities/zombie';

-- All items tagged as forge:ingots/iron
SELECT <-TAGGED_AS<-item.id_str AS members
FROM tag WHERE id_str = 'forge:ingots/iron';

-- What mods add ways to produce iron?
SELECT ->PRODUCED_BY->recipe.mod AS mods
FROM item WHERE id_str = 'minecraft:iron_ingot';
```

## Supported modpack formats

| MC Version | Loader | Status |
|-----------|--------|--------|
| 1.26.x (NeoForge) | NeoForge | ✅ Full support |
| 1.21.x | NeoForge / Fabric | ✅ Full support |
| 1.20.x | NeoForge / Forge / Fabric | ✅ Full support |
| 1.16.x | Forge | ✅ Full support |
| 1.14–1.19 | Any | ✅ Static extraction |
| < 1.13 | Forge | ⚠️ 0 recipes (pre-datapack era, recipes are Java-only) |

## Null result reporting

The build step reports recipe types where `result_item` could not be resolved:

```
⚠ 696 recipes have result_item=None (6.9%). Top types:
     96  tconstruct:material
     64  tconstruct:modifier
    ...
```

These are typically machine-specific schemas not yet handled, or recipes that
genuinely produce no item (tool modifiers, material definitions). The recipe
nodes are still written — only the `PRODUCED_BY` edge is absent.

## Development

```bash
git clone https://github.com/gabrielferrer/mckg-build
cd mckg-build
pip install -e ".[dev]"
pytest
```
