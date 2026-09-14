"""Small Click commands; implementation belongs in the named pipeline steps."""

import sqlite3
import subprocess
from collections.abc import Callable
from functools import wraps
from pathlib import Path
from typing import Concatenate, ParamSpec, TypeVar
from zipfile import BadZipFile

import click

from . import inputs
from .logging import log_session
from .pipeline import Pipeline
from .preview import LanduseSelection
from .settings import PROFILES, Settings
from .state import pipeline_lock

P = ParamSpec("P")
R = TypeVar("R")


@click.group()
@click.option(
    "--project-dir",
    type=click.Path(path_type=Path, exists=True, file_okay=False),
    default=lambda: Path(__file__).resolve().parents[2],
    help="Repository root.",
)
@click.option(
    "--env-file",
    type=click.Path(path_type=Path, exists=True, dir_okay=False),
    help="Defaults to <project-dir>/.env.",
)
@click.pass_context
def main(ctx: click.Context, project_dir: Path, env_file: Path | None) -> None:
    """Build WDR tiles. Configure .env, then run: uv run tiles run."""
    try:
        ctx.obj = Pipeline(Settings.load(project_dir, env_file))
    except (
        OSError,
        ValueError,
        sqlite3.Error,
        subprocess.SubprocessError,
        BadZipFile,
    ) as error:
        raise click.ClickException(str(error)) from error


def locked(command: Callable[Concatenate[Pipeline, P], R]) -> Callable[P, R]:
    @click.pass_obj
    @wraps(command)
    def invoke(pipeline: Pipeline, *args: P.args, **kwargs: P.kwargs) -> R:
        try:
            with (
                pipeline_lock(pipeline.settings.output_dir),
                log_session(
                    pipeline.settings,
                    click.get_current_context().command.name or "unknown",
                ),
            ):
                return command(pipeline, *args, **kwargs)
        except (
            OSError,
            ValueError,
            sqlite3.Error,
            subprocess.SubprocessError,
            BadZipFile,
        ) as error:
            raise click.ClickException(str(error)) from error

    return invoke


@main.command()
@click.pass_obj
def plan(pipeline: Pipeline) -> None:
    """Show configuration and build order without changing files."""
    s = pipeline.settings
    click.echo(f"Repository: {s.root}\nOutput: {s.output_dir}")
    click.echo(
        f"Built-up workers: {s.built_up_workers}; closing z6–9: 100/75/50/25 metres"
    )
    click.echo(f"Threads: {s.threads}; fast: {s.fast}; storage: {s.store or 'RAM'}")
    click.echo(
        f"Geofabrik snapshot: {s.geofabrik_date or 'disabled (use renumbered inputs)'}"
    )
    click.echo(f"Europe: {s.europe}\nDACH: {s.dach}\nNRW extract: {s.nrw}")
    click.echo(
        "Order: setup → download → regions → extract-nrw → coastline → built-up masks → europe → dach → nrw → merge"
    )
    click.echo("Matching completed steps are reused. No overzoom tiles are generated.")


@main.command()
@locked
def run(pipeline: Pipeline) -> None:
    """Prepare everything, build all regions, and merge a candidate MBTiles."""
    pipeline.run()


@main.command()
@locked
def setup(pipeline: Pipeline) -> None:
    """Prepare native tools (apt installation requires the .env opt-in)."""
    pipeline.setup()


@main.command()
@locked
def download(pipeline: Pipeline) -> None:
    """Prepare configured OSM inputs and static shapes; reuse completed files."""
    pipeline.download()


@main.command()
@click.argument("region", type=click.Choice(["all", "europe", "dach"]), default="all")
@locked
def prepare_inputs(pipeline: Pipeline, region: str) -> None:
    """Download dated Geofabrik extracts and renumber them (GEOFABRIK_DATE)."""
    if not pipeline.settings.geofabrik_date:
        raise click.ClickException(
            "Set GEOFABRIK_DATE=YYMMDD in .env or the shell first."
        )
    profiles = ("europe", "dach") if region == "all" else (region,)
    inputs.prepare_geofabrik(pipeline.settings, profiles)


@main.command()
@locked
def extract_nrw(pipeline: Pipeline) -> None:
    """Extract and renumber the padded NRW buffer from Europe."""
    pipeline.extract_nrw()


@main.command()
@locked
def prepare_built_up(pipeline: Pipeline) -> None:
    """Prepare reusable Europe overview masks (also automatic in build europe/run)."""
    pipeline.prepare_built_up()


@main.command()
@locked
def regions(pipeline: Pipeline) -> None:
    """Build Germany/state/PLZ overlays and outside masks separately from the basemap."""
    pipeline.build_regions()


@main.command()
@click.argument("profile", type=click.Choice(PROFILES))
@locked
def build(pipeline: Pipeline, profile: str) -> None:
    """Build one regional or static tileset."""
    pipeline.build(profile)


@main.command()
@locked
def merge(pipeline: Pipeline) -> None:
    """Validate and combine the four builds, with whole-tile NRW priority."""
    pipeline.merge()


@main.command()
@click.option(
    "--before",
    required=True,
    type=click.Path(path_type=Path, exists=True, dir_okay=False),
)
@click.option(
    "--directory",
    required=True,
    type=click.Path(path_type=Path),
    help="New directory for preview artifacts.",
)
@click.option("--bbox", default="6.6,51.2,7.8,51.8", show_default=True)
@click.option(
    "--landuse",
    type=click.Choice(["residential", "built-up"]),
    default="built-up",
    show_default=True,
    help="Built-up compares the overview mask and original classes at z6–11. Residential only omits the combined overview mask.",
)
@locked
def preview(
    pipeline: Pipeline,
    before: Path,
    directory: Path,
    bbox: str,
    landuse: LanduseSelection,
) -> None:
    """Build a small extract and an HTML land-use geometry comparison."""
    pipeline.preview(before.resolve(), directory.resolve(), bbox, landuse)
