"""Repair encoded built-up overview polygons before publishing Europe's tiles.

Valid closing masks can acquire self-intersections during Tilemaker's geometry
operations. Repair the final integer geometry, where those defects can send
MapLibre's triangulator into a very slow fallback. Do not generalize shapes or
rewrite any other layer, feature property, or zoom level.
"""

import gzip
import json
import sqlite3
import time
from collections.abc import Iterator
from concurrent.futures import FIRST_COMPLETED, Future, ProcessPoolExecutor, wait
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

import shapely
from shapely.geometry import Polygon

from . import mvt
from .closing_geometry import RADII, polygons
from .logging import log
from .state import HEARTBEAT_SECONDS, digest

Tile = tuple[int, int, int]  # z, x, TMS y (SQLite's convention)


@dataclass(frozen=True)
class RepairResult:
    data: bytes
    invalid_polygons: int


@dataclass
class RepairStats:
    tiles_checked: int = 0
    tiles_changed: int = 0
    polygons_repaired: int = 0


def build_record() -> dict[str, object]:
    return {
        "version": 1,
        "code_sha256": digest(__file__),
        "mvt_sha256": digest(mvt.__file__),
        "shapely": shapely.__version__,
        "geos": shapely.geos_version_string,
        "zooms": sorted(RADII),
    }


def repair_geometry(data: bytes) -> RepairResult:
    repaired: list[Polygon] = []
    invalid = 0
    for polygon in mvt.polygons(data):
        if polygon.is_valid:
            repaired.append(polygon)
            continue
        invalid += 1
        # Structure respects exterior/hole roles; linework can fill outside
        # holes in these damaged polygons. Collapsed lines have no fill area.
        fixed = shapely.make_valid(polygon, method="structure", keep_collapsed=False)
        fixed = shapely.set_precision(fixed, 1)
        if not fixed.is_valid:
            raise ValueError("Invalid repaired geometry")
        repaired.extend(polygons(fixed))
    if not invalid:
        return RepairResult(data, 0)
    encoded = mvt.encode_polygons(repaired)
    decoded = mvt.polygons(encoded)
    if len(decoded) != len(repaired) or any(not p.is_valid for p in decoded):
        raise ValueError("Invalid geometry after encoding")
    if any(not a.equals(b) for a, b in zip(repaired, decoded, strict=True)):
        raise ValueError("Geometry changed during encoding")
    return RepairResult(encoded, invalid)


def repair_layer(data: bytes) -> RepairResult:
    fields = list(mvt.fields(data))
    keys = [field.payload.decode() for field in fields if field.tag == 3]
    values = [
        next(
            (f.payload.decode() for f in mvt.fields(field.payload) if f.tag == 1),
            None,
        )
        for field in fields
        if field.tag == 4
    ]
    output = bytearray()
    invalid = 0
    for field in fields:
        if field.tag != 2:
            output += field.raw
            continue
        feature = list(mvt.fields(field.payload))
        tags = [n for f in feature if f.tag == 2 for n in mvt.numbers(f.payload)]
        if len(tags) % 2:
            raise ValueError("Unpaired MVT feature tag")
        built_up = any(
            keys[key] == "class" and values[value] == "built_up"
            for key, value in zip(tags[::2], tags[1::2], strict=True)
        )
        is_polygon = any(f.tag == 3 and mvt.numbers(f.payload) == [3] for f in feature)
        if not built_up or not is_polygon:
            output += field.raw
            continue
        new_feature = bytearray()
        changed = False
        for item in feature:
            if item.tag == 4:
                result = repair_geometry(item.payload)
                invalid += result.invalid_polygons
                changed |= bool(result.invalid_polygons)
                new_feature += (
                    mvt.message(item.tag, result.data)
                    if result.invalid_polygons
                    else item.raw
                )
            else:
                new_feature += item.raw
        output += mvt.message(2, bytes(new_feature)) if changed else field.raw
    return RepairResult(bytes(output), invalid)


def repair_tile(data: bytes) -> RepairResult:
    compressed = data[:2] == b"\x1f\x8b"
    raw = gzip.decompress(data) if compressed else data
    output = bytearray()
    invalid = 0
    for field in mvt.fields(raw):
        if field.tag != 3 or field.wire != 2:
            output += field.raw
            continue
        name = next((f.payload for f in mvt.fields(field.payload) if f.tag == 1), b"")
        if name != b"landuse":
            output += field.raw
            continue
        result = repair_layer(field.payload)
        invalid += result.invalid_polygons
        output += (
            mvt.message(field.tag, result.data)
            if result.invalid_polygons
            else field.raw
        )
    if not invalid:
        return RepairResult(data, 0)
    result_data = gzip.compress(bytes(output), mtime=0) if compressed else bytes(output)
    return RepairResult(result_data, invalid)


def _results(
    db: sqlite3.Connection, tiles: list[Tile], workers: int
) -> Iterator[tuple[Tile, RepairResult]]:
    def load(tile: Tile) -> bytes:
        return db.execute(
            "SELECT tile_data FROM tiles WHERE zoom_level=? AND tile_column=? AND tile_row=?",
            tile,
        ).fetchone()[0]

    if workers == 1:
        for tile in tiles:
            try:
                yield tile, repair_tile(load(tile))
            except Exception as error:
                z, x, y = tile
                raise ValueError(
                    f"Overview repair failed at {z}/{x}/{2**z - 1 - y}"
                ) from error
        return

    with ProcessPoolExecutor(max_workers=workers) as pool:
        pending: dict[Future[RepairResult], Tile] = {}
        remaining = iter(tiles)
        try:
            while True:
                # Bound the serialized tile data and immediately replace finished
                # jobs; slow tiles cannot starve otherwise available workers.
                while len(pending) < 2 * workers:
                    tile = next(remaining, None)
                    if tile is None:
                        break
                    pending[pool.submit(repair_tile, load(tile))] = tile
                if not pending:
                    break
                ready, _ = wait(
                    pending, timeout=HEARTBEAT_SECONDS, return_when=FIRST_COMPLETED
                )
                if not ready:
                    log.info("overview_repair.running", in_flight=len(pending))
                for future in ready:
                    tile = pending.pop(future)
                    try:
                        yield tile, future.result()
                    except Exception as error:
                        z, x, y = tile
                        raise ValueError(
                            f"Overview repair failed at {z}/{x}/{2**z - 1 - y}"
                        ) from error
        finally:
            for future in pending:
                future.cancel()


def repair_overviews(path: Path, workers: int = 1) -> RepairStats:
    """Update an unpublished Tilemaker database in one rollback-safe transaction.

    This accepts Tilemaker's flat tiles table, not a published tile-join view. The
    pipeline owns the temporary file and only publishes it after this step passes.
    """
    if workers < 1:
        raise ValueError("Overview repair requires at least one worker")
    stats = RepairStats()
    started = last_progress = time.monotonic()
    try:
        with closing(sqlite3.connect(path)) as db, db:
            schema = db.execute(
                "SELECT type FROM sqlite_master WHERE name='tiles'"
            ).fetchone()
            if schema != ("table",):
                raise ValueError("Overview repair requires a Tilemaker tiles table")
            # Snapshot only keys, not blobs, before any mutation of the table.
            # A single transaction prevents partially repaired output on failure.
            db.execute("BEGIN IMMEDIATE")
            tiles: list[Tile] = db.execute(
                "SELECT zoom_level,tile_column,tile_row FROM tiles "
                "WHERE zoom_level BETWEEN ? AND ? ORDER BY zoom_level,tile_column,tile_row",
                (min(RADII), max(RADII)),
            ).fetchall()
            workers = min(workers, 16, max(1, len(tiles)))
            log.info(
                "overview_repair.started",
                path=str(path),
                tiles=len(tiles),
                workers=workers,
            )
            for tile, result in _results(db, tiles, workers):
                stats.tiles_checked += 1
                if result.invalid_polygons:
                    db.execute(
                        "UPDATE tiles SET tile_data=? WHERE zoom_level=? AND tile_column=? AND tile_row=?",
                        (result.data, *tile),
                    )
                    stats.tiles_changed += 1
                    stats.polygons_repaired += result.invalid_polygons
                if time.monotonic() - last_progress >= HEARTBEAT_SECONDS:
                    log.info(
                        "overview_repair.progress",
                        **vars(stats),
                        total=len(tiles),
                        elapsed_seconds=round(time.monotonic() - started, 1),
                    )
                    last_progress = time.monotonic()
            db.execute("DELETE FROM metadata WHERE name='wdr:geometry_repair'")
            db.execute(
                "INSERT INTO metadata(name,value) VALUES (?,?)",
                (
                    "wdr:geometry_repair",
                    json.dumps(
                        {"build": build_record(), **vars(stats)}, sort_keys=True
                    ),
                ),
            )
    except Exception:
        log.exception("overview_repair.failed", path=str(path), **vars(stats))
        raise
    log.info(
        "overview_repair.completed",
        path=str(path),
        **vars(stats),
        elapsed_seconds=round(time.monotonic() - started, 1),
    )
    return stats
