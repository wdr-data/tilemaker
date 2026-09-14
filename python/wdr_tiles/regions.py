"""Build a separately served German regions MBTiles, without rebuilding the basemap."""

import json
import shutil
import sqlite3
import time

import pyproj
import shapely

from . import regions_geometry as geometry
from . import regions_repair, tile_repair
from .logging import log
from .settings import Settings
from .state import digest, file_record, publish, reusable, run_command, workspace


def build_record(settings: Settings) -> dict[str, object]:
    tools = {}
    for name, command in (
        ("osmium", settings.osmium),
        ("tilemaker", settings.tilemaker),
    ):
        executable = shutil.which(command)
        if not executable:
            raise ValueError(f"Missing {command}; run tiles setup first")
        tools[name] = digest(executable)
    return {
        "version": 1,
        "step": "regions",
        "input": file_record(settings.dach),
        "maxzoom": settings.regions_maxzoom,
        "countries": ["DE"],
        "code": digest(__file__),
        "geometry": digest(geometry.__file__),
        "repair": digest(regions_repair.__file__),
        "repair_dependencies": tile_repair.build_record(),
        "lua": digest(settings.root / "resources/process-regions.lua"),
        "shapely": shapely.__version__,
        "geos": shapely.geos_version_string,
        "pyproj": pyproj.__version__,
        "proj": pyproj.proj_version_str,
        "tools": tools,
    }


def build(settings: Settings) -> None:
    record = build_record(settings)
    destination = settings.output_dir / "regions.mbtiles"
    masks = {
        code: settings.output_dir / f"regions-outside-{code}.geojson"
        for code in ("DE", "DE-NW")
    }
    outputs = [destination, *masks.values()]
    statuses = [reusable(path, record) for path in outputs]
    if all(statuses):
        return
    started = time.monotonic()
    log.info(
        "regions.started", input=str(settings.dach), maxzoom=settings.regions_maxzoom
    )
    with workspace(destination) as work:
        filtered = work / "boundaries.osm.pbf"
        exported = work / "boundaries.geojsonl"
        run_command(
            settings,
            "regions-filter",
            [
                settings.osmium,
                "tags-filter",
                settings.dach,
                "r/admin_level=2,4",
                "r/boundary=postal_code",
                "--remove-tags",
                "--output",
                filtered,
            ],
        )
        run_command(
            settings,
            "regions-export",
            [
                settings.osmium,
                "export",
                filtered,
                "--geometry-types=polygon",
                "--attributes=type,id",
                "--output-format=geojsonseq",
                "-x",
                "print_record_separator=false",
                "--output",
                exported,
            ],
        )
        regions = geometry.read_regions(exported)
        layers = geometry.write_zoom_sources(regions, work, settings.regions_maxzoom)
        country = next(r for r in regions if r.kind == "country")
        west, south, east, north = country.geometry.bounds
        # Only German region polygons are tiled. Inverse world masks are separate
        # GeoJSON assets, avoiding millions of empty-world high-zoom tiles.
        config = {
            "layers": layers,
            "settings": {
                "minzoom": 0,
                "maxzoom": settings.regions_maxzoom,
                "basezoom": settings.regions_maxzoom,
                "include_ids": False,
                "combine_below": 0,
                "compress": "gzip",
                "name": "WDR regions",
                "version": "1.0",
                "description": "Germany, federal states and OSM postal areas",
            },
        }
        config_path = work / "config.json"
        config_path.write_text(json.dumps(config, indent=2))
        output = work / "regions.mbtiles"
        run_command(
            settings,
            "regions-tiles",
            [
                settings.tilemaker,
                "--output",
                output,
                "--config",
                config_path,
                "--process",
                settings.root / "resources/process-regions.lua",
                f"--bbox={west},{south},{east},{north}",
                "--threads",
                settings.threads,
            ],
        )
        regions_repair.repair(output, settings.threads)
        with sqlite3.connect(output) as db:
            if db.execute("PRAGMA quick_check").fetchone() != ("ok",):
                raise ValueError("Regions MBTiles failed SQLite integrity check")
            for key, value in {
                "attribution": "© OpenStreetMap contributors",
                "type": "overlay",
            }.items():
                db.execute("DELETE FROM metadata WHERE name=?", (key,))
                db.execute("INSERT INTO metadata VALUES (?, ?)", (key, value))
        for code in masks:
            region = next(r for r in regions if r.code == code)
            (work / f"regions-outside-{code}.geojson").write_text(
                json.dumps(
                    geometry.mask_collection(region, settings.regions_maxzoom),
                    separators=(",", ":"),
                )
            )
        if build_record(settings) != record:
            raise ValueError(
                "Regions inputs changed during the build; nothing published"
            )
        for path, exists in zip(outputs, statuses, strict=True):
            if not exists:
                publish(work / path.name, path, record)
    log.info(
        "regions.completed",
        output=str(destination),
        bytes=destination.stat().st_size,
        elapsed_seconds=round(time.monotonic() - started, 1),
    )
