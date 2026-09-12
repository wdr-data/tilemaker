"""Parallel polygon preparation feeding a single, batched SQLite writer."""

import json
import sqlite3
import time
from collections import deque
from collections.abc import Generator
from concurrent.futures import Future, ProcessPoolExecutor, TimeoutError
from contextlib import closing
from pathlib import Path
from typing import NamedTuple

import shapely
from shapely.geometry import shape

from . import closing_geometry as geometry
from .logging import log

# Size-hinted reads keep batches small; a single large feature stays intact.
BATCH_BYTES = 4 * 1024 * 1024


class PolygonRow(NamedTuple):
    wkb: bytes
    bounds: geometry.Bounds


class PreparedBatch(NamedTuple):
    rows: list[PolygonRow]
    cells: set[geometry.Cell]
    repaired: int


def prepare_batch(lines: list[bytes]) -> PreparedBatch:
    """Parse, validate and serialize geometry in a worker, without database access."""
    rows: list[PolygonRow] = []
    cells: set[geometry.Cell] = set()
    repaired = 0
    for line in lines:
        feature = json.loads(line)
        if geometry.selected_class(feature["properties"]) is None:
            continue
        polygon = shape(feature["geometry"])
        if not polygon.is_valid:
            polygon = shapely.make_valid(polygon)
            repaired += 1
        for part in geometry.polygons(polygon):
            west, south, east, north = part.bounds
            if not (-180 <= west <= east <= 180 and -85 <= south <= north <= 85):
                raise ValueError(f"Unsupported built-up geometry bounds: {part.bounds}")
            bounds = (west, south, east, north)
            rows.append(PolygonRow(shapely.to_wkb(part), bounds))
            cells.update(geometry.covering_cells(geometry.padded_bounds(bounds)))
    return PreparedBatch(rows, cells, repaired)


def prepared_batches(
    source: Path, workers: int
) -> Generator[tuple[PreparedBatch, int], None, None]:
    """Consume worker results in input order with at most 2×workers in flight."""
    with source.open("rb") as stream:
        if workers == 1:
            while lines := stream.readlines(BATCH_BYTES):
                yield prepare_batch(lines), stream.tell()
            return
        with ProcessPoolExecutor(max_workers=workers) as pool:
            pending: deque[tuple[Future[PreparedBatch], int]] = deque()
            for _ in range(workers * 2):
                lines = stream.readlines(BATCH_BYTES)
                if not lines:
                    break
                pending.append((pool.submit(prepare_batch, lines), stream.tell()))
            while pending:
                future, offset = pending.popleft()
                while True:
                    try:
                        batch = future.result(timeout=60)
                        break
                    except TimeoutError:
                        log.info("built_up.index_waiting", workers=workers)
                yield batch, offset
                lines = stream.readlines(BATCH_BYTES)
                if lines:
                    pending.append((pool.submit(prepare_batch, lines), stream.tell()))


def index_polygons(
    source: Path, database: Path, workers: int = 1
) -> list[geometry.Cell]:
    """Workers prepare geometry; only this process owns the spatial index."""
    if workers < 1:
        raise ValueError("Indexing requires at least one worker")
    cells: set[geometry.Cell] = set()
    count = repaired = 0
    started = last_log = time.monotonic()
    total_bytes = source.stat().st_size
    log.info("built_up.index_started", workers=workers, source_bytes=total_bytes)
    with closing(sqlite3.connect(database)) as db, db:
        db.execute("PRAGMA journal_mode=OFF")  # Private, disposable scratch index.
        db.execute("PRAGMA cache_size=-65536")  # Up to 64 MiB for SQLite pages.
        db.execute("CREATE TABLE polygons(id INTEGER PRIMARY KEY, geometry BLOB)")
        db.execute("CREATE VIRTUAL TABLE bounds USING rtree(id,minx,maxx,miny,maxy)")
        # Close the generator/pool explicitly even if a database write fails.
        with closing(prepared_batches(source, workers)) as batches:
            for batch, offset in batches:
                first_id = count + 1
                db.executemany(
                    "INSERT INTO polygons VALUES (?,?)",
                    ((i, row.wkb) for i, row in enumerate(batch.rows, first_id)),
                )
                db.executemany(
                    "INSERT INTO bounds VALUES (?,?,?,?,?)",
                    (
                        (i, row.bounds[0], row.bounds[2], row.bounds[1], row.bounds[3])
                        for i, row in enumerate(batch.rows, first_id)
                    ),
                )
                count += len(batch.rows)
                repaired += batch.repaired
                cells.update(batch.cells)
                now = time.monotonic()
                if now - last_log >= 60:
                    log.info(
                        "built_up.indexing",
                        polygons=count,
                        cells=len(cells),
                        workers=workers,
                        processed_bytes=offset,
                        source_bytes=total_bytes,
                        percent=round(100 * offset / max(total_bytes, 1), 1),
                        elapsed_seconds=round(now - started, 1),
                    )
                    last_log = now
    log.info(
        "built_up.indexed",
        polygons=count,
        cells=len(cells),
        repaired=repaired,
        workers=workers,
        elapsed_seconds=round(time.monotonic() - started, 1),
    )
    return sorted(cells)
