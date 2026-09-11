#!/usr/bin/env python3
"""Make lower-priority MBTiles omit coordinates supplied by higher-priority files.

Inputs are ordered from broadest to most detailed. Tile data is copied verbatim;
this is replacement of complete tiles, not geometry deduplication. Originals
are always opened read-only. Print the resulting input paths for tile-join.
"""

import sqlite3
from collections.abc import Sequence
from contextlib import closing
from pathlib import Path

from .logging import log


def readonly(path: str | Path) -> str:
    return Path(path).resolve().as_uri() + "?mode=ro"


def disjoint(sources: Sequence[Path], directory: Path) -> list[Path]:
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    outputs = []
    for index, source in enumerate(sources):
        source = Path(source).resolve()

        with closing(sqlite3.connect(readonly(source), uri=True)) as db:
            higher = sources[index + 1 :]
            for i, path in enumerate(higher):
                db.execute(f"ATTACH DATABASE ? AS higher{i}", (readonly(path),))
            matches = [
                f"""EXISTS (SELECT 1 FROM higher{i}.tiles h
                WHERE h.zoom_level=t.zoom_level AND h.tile_column=t.tile_column
                AND h.tile_row=t.tile_row)"""
                for i in range(len(higher))
            ]
            overlap = " OR ".join(matches) or "0"
            count = db.execute(
                f"SELECT count(*) FROM tiles t WHERE {overlap}"
            ).fetchone()[0]

            if not count:
                outputs.append(source)
                continue

            destination = directory / f"{index}-{source.name}"
            if destination.exists():
                raise FileExistsError(destination)

            db.execute("ATTACH DATABASE ? AS filtered", (str(destination.resolve()),))
            db.execute("CREATE TABLE filtered.metadata (name TEXT, value TEXT)")
            db.execute("INSERT INTO filtered.metadata SELECT * FROM main.metadata")
            db.execute("""CREATE TABLE filtered.tiles (zoom_level INTEGER,
                tile_column INTEGER, tile_row INTEGER, tile_data BLOB,
                PRIMARY KEY (zoom_level, tile_column, tile_row))""")
            db.execute(
                f"INSERT INTO filtered.tiles SELECT t.* FROM main.tiles t WHERE NOT ({overlap})"
            )
            db.commit()

            log.info(
                "merge.overlap_removed",
                source=str(source),
                tiles_omitted=count,
                filtered=str(destination),
            )
            outputs.append(destination.resolve())

    return outputs
