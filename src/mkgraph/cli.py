"""
mckg CLI entry point.

Commands:
  build       — extract data from a mods/ directory and write a SurrealQL file
  dump-setup  — install the KubeJS recipe dump script into a server directory
"""

import click
from pathlib import Path


@click.group()
def main():
    """modpack-knowledge-graph — build a SurrealDB knowledge graph from a Minecraft modpack."""
    pass


@main.command()
@click.option("--server",     default=None,   type=click.Path(exists=True), help="Path to the server data root (contains mods/, kubejs/). Infers --mods and --dump automatically.")
@click.option("--mods",       default=None,   type=click.Path(exists=True), help="Path to the mods/ directory. Required if --server is not given.")
@click.option("--output",     required=True,  type=click.Path(),            help="Output path for the SurrealQL file (e.g. knowledge.surql).")
@click.option("--dump",       default=None,   type=click.Path(exists=True), help="Path to a KubeJS dumps/ directory. Auto-detected from --server if omitted.")
@click.option("--vanilla",    default=None,   type=click.Path(exists=True), help="Optional: path to a mcmeta vanilla data directory. Downloaded automatically if omitted.")
@click.option("--vtp-jar",    default=None,   type=click.Path(exists=True), help="Optional: path to a VillagersTradingPlus JAR. Downloaded automatically if omitted.")
@click.option("--mc-version", default="1.21.1", show_default=True,          help="Minecraft version (used for auto-downloading vanilla data).")
@click.option("--loader",     default="neoforge", show_default=True,        help="Mod loader (neoforge or fabric).")
@click.option("--workers",    default=4,      show_default=True,            help="Number of parallel JAR extraction workers.")
def build(server, mods, output, dump, vanilla, vtp_jar, mc_version, loader, workers):
    """Extract modpack data and write a SurrealDB knowledge graph.

    Either --server (points to the server data root) or --mods must be provided.
    --server also auto-detects the KubeJS dump directory.
    """
    from mkgraph.build import run_build

    # Resolve mods and dump from --server if provided
    if server:
        server_path = Path(server)
        mods_path   = Path(mods)   if mods else server_path / "mods"
        dump_path   = Path(dump)   if dump else (
            server_path / "kubejs" / "dumps"
            if (server_path / "kubejs" / "dumps").exists() else None
        )
    elif mods:
        mods_path = Path(mods)
        dump_path = Path(dump) if dump else None
    else:
        raise click.UsageError("Provide either --server or --mods.")

    if not mods_path.exists():
        raise click.UsageError(f"mods directory not found: {mods_path}")

    run_build(
        mods_dir=mods_path,
        output=Path(output),
        dump_dir=dump_path,
        vanilla_dir=Path(vanilla) if vanilla else None,
        vtp_jar=Path(vtp_jar)     if vtp_jar else None,
        mc_version=mc_version,
        loader=loader,
        workers=workers,
    )


@main.command()
@click.option("--server",     required=True,  type=click.Path(exists=True), help="Path to the root of the extracted server data directory (contains mods/, kubejs/, etc.).")
@click.option("--mc-version", default="",     show_default=False,           help="Minecraft version. Auto-detected from mod metadata if omitted.")
@click.option("--loader",     default="neoforge", show_default=True,        help="Mod loader (neoforge or fabric).")
@click.option("--force",      is_flag=True,   default=False,                help="Skip confirmation prompts and overwrite existing files.")
def dump_setup(server, mc_version, loader, force):
    """Install KubeJS (if needed) and drop the recipe dump script into the server.

    Does NOT start or stop the server — that is the user's responsibility.
    After running this, start the server, wait for the dump to complete,
    stop it, then run mckg build.
    """
    from mkgraph.dump_setup import run_dump_setup
    run_dump_setup(
        server_dir=Path(server),
        mc_version=mc_version,
        loader=loader,
        force=force,
    )
