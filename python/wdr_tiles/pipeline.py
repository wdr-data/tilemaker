"""The build itself, in execution order. Each public method is one CLI step."""

import json
import shutil
from pathlib import Path
from typing import NotRequired, TypedDict

from . import closing as built_up
from . import inputs, preview, regions, setup, tile_repair
from .logging import log
from .mbtiles import disjoint
from .settings import EXTRACT_BOUNDS, NRW_BOUNDS, PROFILES, Settings
from .state import (
    CommandArg,
    FileRecord,
    digest,
    file_record,
    publish,
    reusable,
    run_command,
    workspace,
)


class LayerConfig(TypedDict, total=False):
    source: str


class TileConfig(TypedDict):
    layers: dict[str, LayerConfig]
    settings: dict[str, object]


class TileBuildRecord(TypedDict):
    version: int
    step: str
    config_sha256: str
    lua_sha256: str
    input: FileRecord | None
    static_files: list[FileRecord]
    tilemaker_sha256: str
    bbox: str | None
    overview_repair: NotRequired[dict[str, object]]


def executable_record(command: str) -> str:
    resolved = shutil.which(command)
    if not resolved:
        raise ValueError(
            f"Missing executable {command}; run `uv run tiles setup` first."
        )
    return digest(resolved)


class Pipeline:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def setup(self) -> None:
        self.settings = setup.native_tools(self.settings)

    def download(self) -> None:
        inputs.source_data(self.settings)

    def run(self) -> None:
        """One full run, with sequential regions to keep peak memory predictable."""
        self.setup()
        self.download()
        self.build_regions()
        self.extract_nrw()
        self.build("coastline")
        self.build("europe")
        self.build("dach")
        self.build("nrw")
        self.merge()

    def build_regions(self) -> None:
        regions.build(self.settings)

    def extract_nrw(self) -> None:
        settings = self.settings
        record = {
            "version": 1,
            "step": "extract-nrw",
            "input": file_record(settings.europe),
            "bbox": EXTRACT_BOUNDS,
            "osmium": executable_record(settings.osmium),
        }
        if reusable(settings.nrw, record):
            return
        with workspace(settings.nrw) as work:
            extract = work / "extract.osm.pbf"
            output = work / "renumbered.osm.pbf"
            run_command(
                settings,
                "extract-nrw",
                [
                    settings.osmium,
                    "extract",
                    "--bbox",
                    EXTRACT_BOUNDS,
                    "--strategy",
                    "smart",
                    "--set-bounds",
                    settings.europe,
                    "--output",
                    extract,
                ],
            )
            run_command(
                settings,
                "renumber-nrw",
                [settings.osmium, "renumber", extract, "--output", output],
            )
            if file_record(settings.europe) != record["input"]:
                raise ValueError(
                    "Europe input changed while NRW was extracted; wait for renumbering to finish."
                )
            publish(output, settings.nrw, record)

    def prepare_built_up(self) -> None:
        built_up.prepare(
            self.settings, self.settings.europe, self.settings.output_dir / "built-up"
        )

    def build_record(self, profile: str) -> TileBuildRecord:
        settings = self.settings
        config: TileConfig = json.loads(settings.config(profile).read_text())
        static_files = []
        for layer in config["layers"].values():
            if "source" in layer:
                source = settings.root / layer["source"]
                static_files.extend(
                    file_record(source.with_suffix(ext))
                    for ext in setup.SHAPE_EXTENSIONS
                )
        if profile == "europe":
            directory = settings.output_dir / "built-up"
            manifest = built_up.validate(
                directory, built_up.build_record(settings, settings.europe)
            )
            static_files.extend(manifest["files"])
            static_files.append(file_record(directory / "manifest.json"))
        record: TileBuildRecord = {
            "version": 2,
            "step": profile,
            "config_sha256": digest(settings.config(profile)),
            "lua_sha256": digest(settings.process(profile)),
            "input": None
            if profile == "coastline"
            else file_record(settings.input(profile)),
            "static_files": static_files,
            "tilemaker_sha256": executable_record(settings.tilemaker),
            "bbox": {"nrw": NRW_BOUNDS, "coastline": "-180,-85,180,85"}.get(profile),
        }

        if profile == "europe":
            record["overview_repair"] = tile_repair.build_record()
        return record

    def build(self, profile: str) -> None:
        settings = self.settings
        if profile == "europe":
            self.prepare_built_up()
        record = self.build_record(profile)
        destination = settings.output(profile)
        if reusable(destination, record):
            return
        with workspace(destination) as work:
            output = work / "output.mbtiles"
            config_path = settings.config(profile)
            if profile == "europe":
                config: TileConfig = json.loads(config_path.read_text())
                directory = settings.output_dir / "built-up"
                manifest = built_up.validate(
                    directory, built_up.build_record(settings, settings.europe)
                )
                built_up.add_layers(config, directory, manifest)
                config_path = work / "config.json"
                config_path.write_text(json.dumps(config, indent=2))
            args: list[CommandArg] = [
                settings.tilemaker,
                "--output",
                output,
                "--config",
                config_path,
                "--process",
                settings.process(profile),
                "--threads",
                settings.threads,
            ]
            if profile != "coastline":
                args += ["--input", settings.input(profile), "--compact"]
                if settings.fast:
                    args += ["--fast"]
                if settings.store:
                    # Tilemaker's temporary stores are isolated per attempt.
                    settings.store.mkdir(parents=True, exist_ok=True)
                    # A context below removes the stores even after a failed build.
            if record["bbox"]:
                args += [f"--bbox={record['bbox']}"]
            if settings.store and profile != "coastline":
                # Isolate stores per attempt and remove them even after a failure.
                with workspace(settings.store / profile) as storage:
                    run_command(
                        settings,
                        f"build-{profile}",
                        [*args, "--store", storage / "store"],
                    )
            else:
                run_command(settings, f"build-{profile}", args)
            if profile == "europe":
                tile_repair.repair_overviews(output, settings.built_up_workers)
            if record != self.build_record(profile):
                raise ValueError(
                    f"Inputs/configuration changed during {profile}; output was not published."
                )
            publish(output, destination, record)
        log.info("tileset.built", path=str(destination))

    def merge(self) -> None:
        settings = self.settings
        sources = [settings.output(profile) for profile in PROFILES]
        for profile, path in zip(PROFILES, sources, strict=True):
            if not reusable(path, self.build_record(profile)):
                raise ValueError(f"Missing {path}; build it first.")
        destination = settings.output_dir / "nrw-v4.mbtiles"
        record = {
            "version": 1,
            "step": "merge",
            "inputs": [file_record(path) for path in sources],
            "tile_join_sha256": executable_record(settings.tile_join),
        }
        if reusable(destination, record):
            return
        with workspace(destination) as work:
            log.info("merge.filtering", priority=["nrw", "dach", "europe"])
            regional = disjoint(sources[1:], work / "disjoint")
            output = work / "merged.mbtiles"
            run_command(
                settings,
                "merge",
                [
                    settings.tile_join,
                    "--no-tile-size-limit",
                    "-o",
                    output,
                    sources[0],
                    *regional,
                ],
            )
            if record["inputs"] != [file_record(path) for path in sources]:
                raise ValueError(
                    "A source tileset changed during merge; output was not published."
                )
            publish(output, destination, record)
        log.info("candidate.ready", path=str(destination))

    def preview(
        self,
        before: Path,
        directory: Path,
        bbox: str,
        landuse: preview.LanduseSelection = "built-up",
    ) -> None:
        settings = self.settings
        coordinates = tuple(map(float, bbox.split(",")))
        if len(coordinates) != 4:
            raise ValueError("BBOX must be west,south,east,north")
        west, south, east, north = coordinates
        if not (-180 <= west < east <= 180 and -85 <= south < north <= 85):
            raise ValueError(
                "BBOX must have ordered longitude/latitude bounds within ±180/±85"
            )
        if directory.exists():
            raise ValueError(
                f"Preview directory already exists: {directory}; choose a new one."
            )
        directory.mkdir(parents=True)
        extract, renumbered = (
            directory / "extract.osm.pbf",
            directory / "renumbered.osm.pbf",
        )
        run_command(
            settings,
            "preview-extract",
            [
                settings.osmium,
                "extract",
                "--bbox",
                bbox,
                "--strategy",
                "smart",
                "--set-bounds",
                settings.dach,
                "--output",
                extract,
            ],
        )
        run_command(
            settings,
            "preview-renumber",
            [settings.osmium, "renumber", extract, "--output", renumbered],
        )
        config: TileConfig = json.loads(settings.config("europe").read_text())
        config["settings"].update(
            minzoom=6, maxzoom=11 if landuse == "built-up" else 9, basezoom=12
        )
        mask_directory = directory / "built-up"
        manifest = built_up.prepare(settings, renumbered, mask_directory)
        built_up.add_layers(config, mask_directory, manifest)
        config_path = directory / "config.json"
        config_path.write_text(json.dumps(config, indent=2))
        after = directory / "after.mbtiles"
        run_command(
            settings,
            "preview-build",
            [
                settings.tilemaker,
                "--input",
                renumbered,
                "--output",
                after,
                "--config",
                config_path,
                "--process",
                settings.process("europe"),
                "--bbox",
                bbox,
                "--compact",
                "--threads",
                settings.threads,
            ],
        )
        tile_repair.repair_overviews(after, settings.built_up_workers)
        preview.render(
            before,
            after,
            directory / "comparison.html",
            coordinates,
            settings.tile_decode,
            landuse,
        )
