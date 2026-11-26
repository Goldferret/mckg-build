"""
mkgraph dump-setup logic.

Steps:
  1. Detect MC version and loader from mods/ metadata.
  2. Check whether KubeJS is present in mods/.
     If not, download the matching version from Modrinth and place it there.
     Print a note about Architectury if it also seems absent (required by KubeJS).
  3. Write the bundled dump script to kubejs/server_scripts/zz_recipe_dump.js,
     skipping if it's already identical.

Server lifecycle is entirely the user's responsibility.
"""

from __future__ import annotations

import hashlib
import shutil
from importlib.resources import files
from pathlib import Path

import httpx
from rich.console import Console
from rich.prompt import Confirm

from mkgraph.build import _detect_mc_version
from mkgraph.jar_utils import build_installed_modids, detect_loader

console = Console()

MODRINTH_VERSION_URL = "https://api.modrinth.com/v2/project/{project}/version"
KUBEJS_PROJECT   = "mOYnp0Mg"
ARCHITECTURY_PROJECT = "lhGA9TYQ"

DUMP_SCRIPT_NAME = "zz_recipe_dump.js"


def _find_kubejs(mods_dir: Path) -> Path | None:
    """Return the core KubeJS JAR if it exists, else None."""
    for jar in mods_dir.glob("*.jar"):
        name = jar.name.lower()
        # Match "kubejs-neoforge-..." but not addons like "occultism_kubejs-..."
        if name.startswith("kubejs-") or name.startswith("kubejs_"):
            return jar
    return None


def _has_architectury(mods_dir: Path) -> bool:
    return any(
        "architectury" in jar.name.lower()
        for jar in mods_dir.glob("*.jar")
    )


def _download_mod(project_id: str, label: str,
                  mc_version: str, loader: str, dest_dir: Path) -> Path | None:
    """Download the most recent matching release from Modrinth into dest_dir."""
    try:
        resp = httpx.get(
            MODRINTH_VERSION_URL.format(project=project_id), timeout=30
        )
        resp.raise_for_status()
        for v in resp.json():
            if mc_version in v.get("game_versions", []) and loader in v.get("loaders", []):
                for f in v.get("files", []):
                    url  = f.get("url")
                    name = f.get("filename", f"{label}.jar")
                    if url:
                        console.print(f"  Downloading {name}...")
                        jar_path = dest_dir / name
                        with httpx.stream("GET", url, follow_redirects=True, timeout=120) as r:
                            r.raise_for_status()
                            with open(jar_path, "wb") as fp:
                                for chunk in r.iter_bytes(65536):
                                    fp.write(chunk)
                        console.print(f"  [green]✓ {name}[/green]")
                        return jar_path
    except Exception as e:
        console.print(f"  [red]Failed to download {label}: {e}[/red]")
    return None


def _script_path() -> Path:
    """Return the path to the bundled dump script inside the package."""
    return Path(str(files("mkgraph").joinpath(f"scripts/{DUMP_SCRIPT_NAME}")))


def run_dump_setup(server_dir: Path, mc_version: str, loader: str, force: bool) -> None:
    console.rule("[bold blue]mkgraph dump-setup[/bold blue]")

    mods_dir    = server_dir / "mods"
    scripts_dir = server_dir / "kubejs" / "server_scripts"

    if not mods_dir.exists():
        console.print(f"[red]mods/ directory not found at {mods_dir}[/red]")
        console.print("Make sure --server points to the root of the extracted server data.")
        return

    # -- Step 1: Detect version and loader ----------------------------------
    jars = sorted(mods_dir.glob("*.jar"))
    if not mc_version:
        mc_version = _detect_mc_version(jars) or "1.21.1"
    detected_loader = detect_loader(mods_dir)
    if detected_loader != loader:
        console.print(f"  [yellow]Detected loader {detected_loader!r} (overrides --loader {loader!r})[/yellow]")
        loader = detected_loader
    console.print(f"Step 1: Detected MC version: [bold]{mc_version}[/bold], loader: {loader}")

    # -- Step 2: KubeJS ------------------------------------------------------
    kjs = _find_kubejs(mods_dir)
    if kjs:
        console.print(f"Step 2: KubeJS already present — [green]{kjs.name}[/green]")
    else:
        console.print("Step 2: KubeJS not found in mods/.")
        if not force:
            if not Confirm.ask(f"  Download KubeJS for {mc_version} {loader} into {mods_dir}?"):
                console.print("  Skipped. Add KubeJS manually and re-run dump-setup.")
                return
        kjs = _download_mod(KUBEJS_PROJECT, "KubeJS", mc_version, loader, mods_dir)
        if kjs is None:
            console.print("[red]Could not download KubeJS. Aborting.[/red]")
            return

        # Check Architectury (KubeJS hard dependency)
        if not _has_architectury(mods_dir):
            console.print(
                "  [yellow]Warning: Architectury API not found in mods/.[/yellow]\n"
                "  KubeJS requires it. Downloading..."
            )
            arch = _download_mod(ARCHITECTURY_PROJECT, "Architectury", mc_version, loader, mods_dir)
            if arch is None:
                console.print(
                    "  [yellow]Could not auto-download Architectury. "
                    "Add it manually before starting the server.[/yellow]"
                )

    # -- Step 3: Dump script -------------------------------------------------
    scripts_dir.mkdir(parents=True, exist_ok=True)
    dest = scripts_dir / DUMP_SCRIPT_NAME
    src  = _script_path()

    if dest.exists() and not force:
        src_hash  = hashlib.md5(src.read_bytes()).hexdigest()
        dest_hash = hashlib.md5(dest.read_bytes()).hexdigest()
        if src_hash == dest_hash:
            console.print(f"Step 3: Dump script already up to date — {dest}")
        else:
            if Confirm.ask(f"  Dump script at {dest} differs. Overwrite?"):
                shutil.copy2(src, dest)
                console.print(f"Step 3: [green]✓ Script updated at {dest}[/green]")
            else:
                console.print("  Skipped. Existing script kept.")
    else:
        shutil.copy2(src, dest)
        console.print(f"Step 3: [green]✓ Script written to {dest}[/green]")

    # -- Done ----------------------------------------------------------------
    console.rule("[bold green]Setup complete[/bold green]")
    console.print()
    console.print("Next steps:")
    console.print(f"  1. Start your server (the dump script runs automatically on load).")
    console.print(f"  2. Wait for: [bold]\\[RecipeDump] Done: N recipes[/bold] in the log.")
    console.print(f"  3. Stop the server.")
    console.print(f"  4. Run:")
    console.print(f"       [bold]mkgraph build --server {server_dir} --output ./knowledge[/bold]")
    console.print()
    console.print(f"  Dump output will be at: {server_dir / 'kubejs' / 'dumps' / 'recipes_meta.json'}")
