"""
Main build orchestrator for mkgraph build.

Steps:
  1. Discover all JARs
  2. Build installed modid set (for condition evaluation)
  3. Download vanilla data (mcmeta) if not provided
  4. Download VillagersTradingPlus JAR if not provided
  5. Extract tags (vanilla + mods), resolve nested refs
  6. Extract recipes (dump override + JAR static)
  7. Extract loot tables (vanilla + mods)
  8. Extract worldgen / ore spawns (vanilla + mods)
  9. Extract villager trades (VTP vanilla + mod additions)
 10. Write SurrealQL output
"""

from __future__ import annotations

import tempfile
import shutil
from pathlib import Path

import httpx
from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn, TaskProgressColumn

from mkgraph.jar_utils import build_installed_modids, detect_loader
from mkgraph.extract_tags import extract_tags, resolve_tags, resolve_nested
from mkgraph.extract_recipes import extract_recipes
from mkgraph.extract_loot import extract_loot
from mkgraph.extract_worldgen import extract_worldgen
from mkgraph.extract_trades import extract_trades
from mkgraph.db_writer import write_graph

console = Console()

MCMETA_URL  = "https://github.com/misode/mcmeta/archive/refs/tags/{version}-data.tar.gz"
VTP_API_URL = "https://api.modrinth.com/v2/project/villagerstradingplus/version"


def _detect_mc_version(jars: list[Path]) -> str | None:
    """
    Try to detect the Minecraft version from mod metadata in the JARs.
    Returns the first version found, or None if undetectable.

    Handles:
    - Forge/NeoForge mods.toml versionRange (bare or bracket notation)
    - Fabric launcher jar filename (fabric-server-mc.X.Y.Z-...)
    - Fabric mod filenames (mod-name-fabricMC-X.Y.Z.jar)
    - NeoForge installer jar filename (neoforge-X.Y.Z-installer.jar)
    """
    import zipfile, re

    # Pass 1: Fabric launcher jar at top level or in mods/
    # e.g. fabric-server-mc.1.21.1-loader.0.20.1-launcher.1.1.2.jar
    for jar in jars:
        m = re.search(r'fabric-server-mc\.(\d+\.\d+(?:\.\d+)?)', jar.name, re.I)
        if m:
            return m.group(1)

    # Pass 2: scan TOML metadata inside JARs (Forge/NeoForge)
    for jar in jars[:30]:
        try:
            with zipfile.ZipFile(jar) as z:
                for toml_name in ("META-INF/neoforge.mods.toml", "META-INF/mods.toml"):
                    if toml_name not in z.namelist():
                        continue
                    raw = z.read(toml_name).decode("utf-8", errors="replace")
                    # Find the minecraft dependency block; versionRange may be on the next line
                    m = re.search(
                        r'modId\s*=\s*["\']minecraft["\'][\s\S]*?versionRange\s*=\s*["\[]([^"\']+)["\']',
                        raw
                    )
                    if m:
                        raw_range = m.group(1)
                        v = re.search(r'(\d+\.\d+(?:\.\d+)?)', raw_range)
                        if v:
                            return v.group(1)
        except Exception:
            continue

    # Pass 3: infer from mod jar filenames — look for consistent MC version suffix
    # e.g. AdvancedCoreInfo-fabric-1.21.1-1.5.0.jar or BiomesOPlenty-forge-1.20.1-19.0.0.96.jar
    version_counts: dict[str, int] = {}
    mc_pat = re.compile(r'[_\-+\.](1\.\d+(?:\.\d+)?)(?:[_\-+\.]|$)')
    for jar in jars:
        for m in mc_pat.finditer(jar.name):
            v = m.group(1)
            # Filter out likely mod versions (too many segments or starts with known non-mc)
            parts = v.split(".")
            if len(parts) <= 3:
                version_counts[v] = version_counts.get(v, 0) + 1
    if version_counts:
        # Pick the most common version that looks like a MC version (1.X or 1.X.Y)
        # and has significant votes (>10% of jars)
        threshold = max(1, len(jars) // 10)
        candidates = {v: c for v, c in version_counts.items() if c >= threshold}
        if candidates:
            return max(candidates, key=lambda v: candidates[v])

    return None


def _download_vanilla(mc_version: str, dest: Path) -> Path | None:
    """Download and extract the mcmeta vanilla data for the given version.

    Returns the extracted path, or None if the version is not in mcmeta
    (mcmeta tracks Java Edition 1.14+ only; older packs like 1.7.10 return 404).
    """
    url = MCMETA_URL.format(version=mc_version)
    console.print(f"  Downloading vanilla data for {mc_version}...")
    tar_path = dest / f"mcmeta-{mc_version}-data.tar.gz"

    with httpx.stream("GET", url, follow_redirects=True, timeout=120) as r:
        if r.status_code == 404:
            console.print(
                f"  [yellow]Warning: mcmeta has no data for {mc_version} "
                f"(mcmeta tracks 1.14+ only). Continuing without vanilla data.[/yellow]"
            )
            return None
        r.raise_for_status()
        with open(tar_path, "wb") as f:
            for chunk in r.iter_bytes(chunk_size=65536):
                f.write(chunk)

    import tarfile
    with tarfile.open(tar_path) as tf:
        tf.extractall(dest)

    extracted = dest / f"mcmeta-{mc_version}-data"
    if not extracted.exists():
        # Some archives nest differently
        dirs = [d for d in dest.iterdir() if d.is_dir() and "mcmeta" in d.name]
        if dirs:
            extracted = dirs[0]

    console.print(f"  [green]✓ Vanilla data at {extracted}[/green]")
    return extracted


def _download_vtp(mc_version: str, loader: str, dest: Path) -> Path | None:
    """Download the VillagersTradingPlus JAR for the given version.

    Returns the JAR path, or None if no matching release exists for
    this mc_version/loader combination.
    """
    console.print("  Downloading VillagersTradingPlus JAR...")
    try:
        resp = httpx.get(VTP_API_URL, timeout=30)
        resp.raise_for_status()
        versions = resp.json()
        for v in versions:
            if mc_version in v.get("game_versions", []) and loader in v.get("loaders", []):
                for f in v.get("files", []):
                    url = f.get("url")
                    name = f.get("filename", "vtp.jar")
                    if url:
                        jar_path = dest / name
                        with httpx.stream("GET", url, follow_redirects=True, timeout=60) as r:
                            r.raise_for_status()
                            with open(jar_path, "wb") as fp:
                                for chunk in r.iter_bytes(chunk_size=65536):
                                    fp.write(chunk)
                        console.print(f"  [green]✓ VTP JAR at {jar_path}[/green]")
                        return jar_path
        # Fell through the loop — no release matches this mc_version + loader
        console.print(
            f"  [yellow]No VTP JAR available for {mc_version}/{loader}. "
            f"Continuing without vanilla trades.[/yellow]"
        )
    except Exception as e:
        console.print(f"  [yellow]Warning: Could not download VTP JAR: {e}[/yellow]")
    return None


def run_build(
    mods_dir: Path,
    output: Path,
    dump_dir: Path | None,
    vanilla_dir: Path | None,
    vtp_jar: Path | None,
    mc_version: str,
    loader: str,
    workers: int,
) -> None:
    console.rule("[bold blue]modpack-knowledge-graph build[/bold blue]")

    cache_dir = output.parent / ".mkgraph_cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    tmp_dir = Path(tempfile.mkdtemp(prefix="mkgraph_"))

    try:
        # --- Step 1: Discover JARs ---
        jars = sorted(mods_dir.glob("*.jar"))
        # Also include root-level jars (Fabric launcher ships at server root)
        root_jars = sorted(mods_dir.parent.glob("*.jar"))
        all_jars_for_detection = root_jars + jars
        console.print(f"[bold]Step 1:[/bold] Found {len(jars)} JARs in {mods_dir}")

        # --- Step 2: Installed modids + version detection ---
        console.print("[bold]Step 2:[/bold] Building installed mod ID set...")
        installed = build_installed_modids(mods_dir)
        console.print(f"  {len(installed)} mod IDs detected")

        detected_version = _detect_mc_version(all_jars_for_detection)
        if detected_version and detected_version != mc_version:
            console.print(f"  [yellow]Detected MC version {detected_version} from mod metadata "
                          f"(overrides --mc-version {mc_version})[/yellow]")
            mc_version = detected_version
        elif detected_version:
            console.print(f"  Detected MC version: {detected_version}")

        detected_loader = detect_loader(mods_dir)
        if detected_loader != loader:
            console.print(f"  [yellow]Detected loader {detected_loader!r} from mod metadata "
                          f"(overrides --loader {loader!r})[/yellow]")
            loader = detected_loader
        else:
            console.print(f"  Detected loader: {detected_loader}")

        # --- Step 3: Vanilla data ---
        if vanilla_dir is None:
            vanilla_dir = cache_dir / f"mcmeta-{mc_version}-data"
            if not vanilla_dir.exists():
                console.print("[bold]Step 3:[/bold] Downloading vanilla data...")
                downloaded = _download_vanilla(mc_version, cache_dir)
                if downloaded is None:
                    # Version not in mcmeta; use empty dir so extractors skip vanilla gracefully
                    vanilla_dir = tmp_dir / "empty-vanilla"
                    vanilla_dir.mkdir(parents=True, exist_ok=True)
                    console.print("  [yellow]Proceeding without vanilla recipes/tags/loot.[/yellow]")
                else:
                    vanilla_dir = downloaded
            else:
                console.print(f"[bold]Step 3:[/bold] Using cached vanilla data at {vanilla_dir}")
        else:
            console.print(f"[bold]Step 3:[/bold] Using provided vanilla data at {vanilla_dir}")

        # --- Step 4: VTP JAR ---
        if vtp_jar is None:
            vtp_cache = list(cache_dir.glob("villagertradingplus*.jar"))
            if vtp_cache:
                vtp_jar = vtp_cache[0]
                console.print(f"[bold]Step 4:[/bold] Using cached VTP JAR at {vtp_jar}")
            else:
                console.print("[bold]Step 4:[/bold] Downloading VillagersTradingPlus JAR...")
                vtp_jar = _download_vtp(mc_version, loader, cache_dir)
        else:
            console.print(f"[bold]Step 4:[/bold] Using provided VTP JAR at {vtp_jar}")

        # --- Step 5: Tags ---
        console.print("[bold]Step 5:[/bold] Extracting tags...")
        raw_tags = extract_tags(jars, vanilla_dir)
        console.print(f"  {len(raw_tags)} tag contributions collected")
        tags_map = resolve_tags(raw_tags)
        tags_map = resolve_nested(tags_map)
        console.print(f"  {len(tags_map)} unique tags after merge and resolution")

        # --- Step 6: Recipes ---
        console.print("[bold]Step 6:[/bold] Extracting recipes...")
        recipes = extract_recipes(jars, installed, dump_dir)
        dump_count = sum(1 for r in recipes if "dump" in r.get("source", ""))
        jar_count  = sum(1 for r in recipes if r.get("source") == "jar")
        console.print(f"  {len(recipes)} total ({dump_count} from dump, {jar_count} from JARs only)")

        # Null-result audit: surface recipe types where result_item could not be parsed
        null_result = [r for r in recipes if not r.get("result_item")]
        if null_result:
            from collections import Counter
            type_counts = Counter(r.get("type", "unknown") for r in null_result)
            top = type_counts.most_common(10)
            console.print(
                f"  [yellow]⚠ {len(null_result)} recipes have result_item=None "
                f"({len(null_result)/len(recipes)*100:.1f}%). "
                f"Top types:[/yellow]"
            )
            for rtype, cnt in top:
                console.print(f"    {cnt:>6}  {rtype}")

        # --- Step 7: Loot tables ---
        console.print("[bold]Step 7:[/bold] Extracting loot tables...")
        loot = extract_loot(jars, vanilla_dir)
        mob_count   = sum(1 for l in loot if l["category"] == "mob")
        chest_count = sum(1 for l in loot if l["category"] == "chest")
        console.print(f"  {len(loot)} loot tables ({mob_count} mob, {chest_count} chest)")

        # --- Step 8: Worldgen ---
        console.print("[bold]Step 8:[/bold] Extracting ore worldgen...")
        worldgen = extract_worldgen(jars, vanilla_dir, tmp_dir)
        console.print(f"  {len(worldgen)} ore spawn records")

        # --- Step 9: Trades ---
        console.print("[bold]Step 9:[/bold] Extracting villager trades...")
        trades = extract_trades(jars, vtp_jar)
        vanilla_trade_count = sum(1 for t in trades if t["source"] == "vanilla_vtp")
        mod_trade_count     = len(trades) - vanilla_trade_count
        console.print(f"  {len(trades)} trades ({vanilla_trade_count} vanilla, {mod_trade_count} from mods)")

        # --- Step 10: Write ---
        console.print("[bold]Step 10:[/bold] Writing knowledge graph (embedded SurrealDB)...")
        write_graph(
            output=output,
            recipes=recipes,
            tags_resolved=tags_map,
            loot=loot,
            worldgen=worldgen,
            trades=trades,
        )

        console.rule("[bold green]Build complete[/bold green]")

    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)
