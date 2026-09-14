"""Validate and repair region polygons after clipping onto the integer tile grid."""

import gzip
import sqlite3
import time
from concurrent.futures import ProcessPoolExecutor
from contextlib import closing
from pathlib import Path

from . import mvt
from .logging import log
from .state import HEARTBEAT_SECONDS
from .tile_repair import RepairResult, RepairStats, repair_geometry


def repair_tile(data: bytes) -> RepairResult:
    compressed = data.startswith(b"\x1f\x8b")
    raw = gzip.decompress(data) if compressed else data
    output = bytearray()
    invalid = 0
    for layer in mvt.fields(raw):
        if layer.tag != 3 or layer.wire != 2:
            output += layer.raw
            continue
        fields = list(mvt.fields(layer.payload))
        name = next((f.payload for f in fields if f.tag == 1), b"")
        if name != b"regions":
            output += layer.raw
            continue
        rebuilt = bytearray()
        layer_changed = False
        for field in fields:
            if field.tag != 2:
                rebuilt += field.raw
                continue
            feature = list(mvt.fields(field.payload))
            if not any(f.tag == 3 and mvt.numbers(f.payload) == [3] for f in feature):
                rebuilt += field.raw
                continue
            encoded = bytearray()
            changed = False
            for item in feature:
                if item.tag == 4:
                    result = repair_geometry(item.payload)
                    invalid += result.invalid_polygons
                    changed |= bool(result.invalid_polygons)
                    encoded += (
                        mvt.message(4, result.data)
                        if result.invalid_polygons
                        else item.raw
                    )
                else:
                    encoded += item.raw
            rebuilt += mvt.message(2, bytes(encoded)) if changed else field.raw
            layer_changed |= changed
        output += mvt.message(3, bytes(rebuilt)) if layer_changed else layer.raw
    if not invalid:
        return RepairResult(data, 0)
    return RepairResult(
        gzip.compress(bytes(output), mtime=0) if compressed else bytes(output), invalid
    )


def repair_batch(
    rows: list[tuple[int, int, int, bytes]],
) -> list[tuple[int, int, int, RepairResult]]:
    results = []
    for z, x, y, data in rows:
        try:
            results.append((z, x, y, repair_tile(data)))
        except Exception as error:
            raise ValueError(
                f"Region repair failed at {z}/{x}/{2**z - 1 - y}"
            ) from error
    return results


def repair(path: Path, workers: int) -> RepairStats:
    """Repair the unpublished database atomically, with bounded batches in memory."""
    stats = RepairStats()
    started = last_progress = time.monotonic()
    workers = min(max(1, workers), 16)
    with (
        closing(sqlite3.connect(path)) as db,
        db,
        ProcessPoolExecutor(max_workers=workers) as pool,
    ):
        db.execute("BEGIN IMMEDIATE")
        # Tilemaker's flat tiles table has stable rowids; do not keep an active
        # SELECT cursor across updates to that same table.
        last_rowid = 0
        total = db.execute("SELECT count(*) FROM tiles").fetchone()[0]
        log.info("regions.repair_started", tiles=total, workers=workers)
        while True:
            rows = db.execute(
                "SELECT rowid,zoom_level,tile_column,tile_row,tile_data FROM tiles "
                "WHERE rowid>? ORDER BY rowid LIMIT ?",
                (last_rowid, workers * 128),
            ).fetchall()
            if not rows:
                break
            last_rowid = rows[-1][0]
            batches = [
                [tuple(row[1:]) for row in rows[i : i + 128]]
                for i in range(0, len(rows), 128)
            ]
            for results in pool.map(repair_batch, batches):
                for z, x, y, result in results:
                    stats.tiles_checked += 1
                    if result.invalid_polygons:
                        db.execute(
                            "UPDATE tiles SET tile_data=? WHERE zoom_level=? AND tile_column=? AND tile_row=?",
                            (result.data, z, x, y),
                        )
                        stats.tiles_changed += 1
                        stats.polygons_repaired += result.invalid_polygons
            if time.monotonic() - last_progress >= HEARTBEAT_SECONDS:
                log.info("regions.repair_progress", total=total, **vars(stats))
                last_progress = time.monotonic()
    log.info(
        "regions.repair_completed",
        elapsed_seconds=round(time.monotonic() - started, 1),
        **vars(stats),
    )
    return stats
