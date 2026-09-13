"""Prepare reusable, overview masks before the Europe tile build."""

import json
import os
import pickle
import shutil
import time
from collections.abc import Mapping
from concurrent.futures import FIRST_COMPLETED, Future, ProcessPoolExecutor, wait
from contextlib import ExitStack
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import TextIO, TypedDict

import pyproj
import shapely

from . import closing_geometry as geometry
from .indexing import index_polygons
from .logging import log
from .preview import BUILT_UP_CLASSES
from .settings import Settings
from .state import FileRecord, digest, file_record, run_command, workspace


class MaskSource(TypedDict):
    zoom: int
    filename: str


class Manifest(TypedDict):
    build: dict[str, object]
    sources: list[MaskSource]
    files: list[FileRecord]


def build_record(settings: Settings, source: Path) -> dict[str, object]:
    osmium = shutil.which(settings.osmium)
    if osmium is None:
        raise ValueError("Missing osmium; run `uv run tiles setup` first.")
    return {
        "version": 1,
        "input": file_record(source),
        "osmium_sha256": digest(osmium),
        "code_sha256": digest(__file__),
        "geometry_sha256": digest(geometry.__file__),
        "indexing_sha256": digest(Path(__file__).with_name("indexing.py")),
        "classes": sorted(BUILT_UP_CLASSES),
        "shapely": shapely.__version__,
        "geos": shapely.geos_version_string,
        "pyproj": pyproj.__version__,
        "proj": pyproj.proj_version_str,
    }


def validate(directory: Path, record: dict[str, object]) -> Manifest:
    try:
        manifest: Manifest = json.loads((directory / "manifest.json").read_text())
        if manifest["build"] != record:
            raise ValueError("build inputs changed")
        for expected in manifest["files"]:
            if file_record(expected["path"]) != expected:
                raise ValueError(f"mask changed: {expected['path']}")
        expected_files = {
            str(directory / source["filename"]) for source in manifest["sources"]
        }
        if expected_files != {item["path"] for item in manifest["files"]}:
            raise ValueError("incomplete mask inventory")
    except (OSError, KeyError, ValueError) as error:
        raise ValueError(
            f"Stale or incomplete built-up masks in {directory}; choose a new OUTPUT_DIR."
        ) from error
    return manifest


class MaskWriters:
    """Stream one GeoJSONL source per zoom; no shapefile size or layer limits."""

    def __init__(self, directory: Path, stack: ExitStack) -> None:
        self.directory = directory
        self.stack = stack
        self.writers: dict[int, TextIO] = {}
        self.sources: list[MaskSource] = []

    def append(self, masks: dict[int, bytes]) -> None:
        for zoom, wkb in sorted(masks.items()):
            for polygon in geometry.polygons(shapely.from_wkb(wkb)):
                writer = self.writers.get(zoom)
                if writer is None:
                    filename = f"built-up-z{zoom}.geojsonl"
                    writer = self.stack.enter_context(
                        (self.directory / filename).open("w")
                    )
                    self.writers[zoom] = writer
                    self.sources.append({"zoom": zoom, "filename": filename})
                coordinates = shapely.to_geojson(shapely.orient_polygons(polygon))
                writer.write(
                    '{"type":"Feature","properties":{"class":"built_up","built_up":true},"geometry":'
                    + coordinates
                    + "}\n"
                )


def write_masks(
    database: Path, cells: list[geometry.Cell], directory: Path, workers: int
) -> list[MaskSource]:
    """Refill on completion; spool out-of-order results to keep RAM bounded."""
    if workers < 1:
        raise ValueError("Closing requires at least one worker")
    started = time.monotonic()
    with ExitStack() as stack:
        writers = MaskWriters(directory, stack)
        # A slow cell must not hold up scheduling. Completed results wait on
        # disk until they can be appended in the original, stable cell order.
        spool = Path(
            stack.enter_context(TemporaryDirectory(prefix=".cells-", dir=directory))
        )
        with ProcessPoolExecutor(max_workers=workers) as pool:
            jobs = iter(enumerate(cells))
            pending: dict[Future[dict[int, bytes]], tuple[int, geometry.Cell]] = {}
            for _ in range(min(len(cells), workers * 2)):
                index, cell = next(jobs)
                pending[pool.submit(geometry.process_cell, database, cell)] = (
                    index,
                    cell,
                )
            ready: set[int] = set()
            completed = written = 0
            last_log = started
            while pending:
                done, _ = wait(pending, timeout=60, return_when=FIRST_COMPLETED)
                if not done:
                    log.info(
                        "built_up.running",
                        completed=completed,
                        written=written,
                        buffered_cells=len(ready),
                        in_flight=len(pending),
                        oldest_cell=min(pending.values())[1],
                        total=len(cells),
                        elapsed_seconds=round(time.monotonic() - started, 1),
                    )
                for future in done:
                    index, cell = pending.pop(future)
                    try:
                        masks = future.result()
                    except Exception as error:
                        # Report before executor shutdown: waiting silently for
                        # other cells made geometry failures look like a stall.
                        log.error(
                            "built_up.cell_failed",
                            cell=cell,
                            error=str(error),
                            completed=completed,
                            written=written,
                        )
                        for other in pending:
                            other.cancel()
                        active = {other for other in pending if not other.done()}
                        shutdown_started = time.monotonic()
                        while active:
                            log.info(
                                "built_up.stopping",
                                active=len(active),
                                failed_cell=cell,
                                elapsed_seconds=round(
                                    time.monotonic() - shutdown_started, 1
                                ),
                            )
                            _, active = wait(active, timeout=30)
                        raise ValueError(f"Closing failed in cell {cell}") from error
                    with (spool / str(index)).open("wb") as output:
                        pickle.dump(masks, output, protocol=pickle.HIGHEST_PROTOCOL)
                    ready.add(index)
                    completed += 1
                    # Refill for every finished task, even if earlier output
                    # is still missing. The old FIFO loop starved the pool here.
                    job = next(jobs, None)
                    if job is not None:
                        next_index, next_cell = job
                        pending[
                            pool.submit(geometry.process_cell, database, next_cell)
                        ] = next_index, next_cell
                # Futures retain their result until released. Don't keep the
                # completed batch alive while flushing a potentially long spool.
                done.clear()
                while written in ready:
                    path = spool / str(written)
                    with path.open("rb") as source:
                        # Only read results we just wrote inside this private directory.
                        writers.append(pickle.load(source))
                    path.unlink()
                    ready.remove(written)
                    written += 1
                if time.monotonic() - last_log >= 60 or completed == len(cells):
                    log.info(
                        "built_up.progress",
                        completed=completed,
                        written=written,
                        buffered_cells=len(ready),
                        in_flight=len(pending),
                        total=len(cells),
                        files=len(writers.sources),
                        elapsed_seconds=round(time.monotonic() - started, 1),
                    )
                    last_log = time.monotonic()
        return writers.sources


def prepare(settings: Settings, source: Path, directory: Path) -> Manifest:
    record = build_record(settings, source)
    if directory.exists():
        manifest = validate(directory, record)
        log.info("built_up.reused", path=str(directory))
        return manifest
    log.info(
        "built_up.started",
        input=str(source),
        workers=settings.built_up_workers,
        radii_metres=geometry.RADII,
    )
    started = time.monotonic()
    with workspace(directory) as work:
        filtered = work / "selected.osm.pbf"
        geojson = work / "selected.geojsonl"
        classes = ",".join(sorted(BUILT_UP_CLASSES))
        expressions = [
            f"wr/{key}={classes}"
            for key in ("landuse", "natural", "leisure", "amenity", "tourism")
        ]
        highways = ",".join(sorted(geometry.PAVED_HIGHWAYS))
        # Closed highway ways need area=yes; assembled relations imply an area.
        # Filter these forms separately to avoid exporting Europe's road network.
        # tags-filter uses OR; selection applies the exact surface rules later.
        expressions.extend(
            [
                f"wr/area:highway={highways}",
                f"r/highway={highways}",
                "w/area=yes",
                "wr/place=square",
                "wr/amenity=parking,marketplace",
            ]
        )
        run_command(
            settings,
            "built-up-filter",
            [
                settings.osmium,
                "tags-filter",
                source,
                *expressions,
                "--output",
                filtered,
            ],
        )
        run_command(
            settings,
            "built-up-export",
            [
                settings.osmium,
                "export",
                filtered,
                "--geometry-types=polygon",
                "--attributes=type",
                "--output-format=geojsonseq",
                "-x",
                "print_record_separator=false",
                "--output",
                geojson,
            ],
        )
        database = work / "polygons.sqlite"
        cells = index_polygons(geojson, database, settings.built_up_workers)
        filtered.unlink()
        geojson.unlink()
        sources = write_masks(database, cells, work, settings.built_up_workers)
        database.unlink()
        if build_record(settings, source) != record:
            raise ValueError(
                "Built-up inputs or code changed during preparation; output was not published."
            )
        files = []
        for mask_source in sources:
            relative = mask_source["filename"]
            identity = file_record(work / relative)
            identity["path"] = str(directory / relative)
            files.append(identity)
        manifest = {"build": record, "sources": sources, "files": files}
        (work / "manifest.json").write_text(json.dumps(manifest, indent=2))
        # Publish the complete directory together. The CLI's pipeline lock
        # prevents concurrent preparations; an incomplete workspace is discarded.
        if directory.exists():
            raise FileExistsError(directory)
        os.rename(work, directory)
    log.info(
        "built_up.completed",
        path=str(directory),
        cells=len(cells),
        files=len(sources),
        elapsed_seconds=round(time.monotonic() - started, 1),
    )
    return validate(directory, record)


def add_layers(
    config: Mapping[str, object], directory: Path, manifest: Manifest
) -> None:
    """Attach zoom-specific masks to landuse without changing other sources."""
    # Parsed JSON structure is checked explicitly at this boundary.
    layers = config["layers"]
    if not isinstance(layers, dict) or not isinstance(layers.get("landuse"), dict):
        raise ValueError("Config must declare landuse before its built-up aliases")
    template = layers["landuse"]
    for index, source in enumerate(manifest["sources"]):
        zoom = source["zoom"]
        layers[f"landuse_built_up_{index}"] = {
            "minzoom": zoom,
            "maxzoom": zoom,
            "simplify_below": template["simplify_below"],
            "simplify_level": template["simplify_level"],
            "simplify_ratio": template.get("simplify_ratio", 2),
            "combine_polygons_below": 10,
            "source": str(directory / source["filename"]),
            "source_columns": ["class", "built_up"],
            "write_to": "landuse",
        }
    if len(layers) > 256:
        raise ValueError("Config exceeds Tilemaker's 256-layer limit")
