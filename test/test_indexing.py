"""Parallel indexing must preserve geometry, row order and spatial lookup results."""

import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from shapely.geometry import MultiPolygon, Polygon, box, mapping

from wdr_tiles import indexing


class IndexingTest(unittest.TestCase):
    def test_serial_and_parallel_indexes_match_with_repairs_and_multiple_batches(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source.geojsonl"
            geometries = [
                box(6.999, 50.999, 7.002, 51.002),
                MultiPolygon(
                    [box(6.9, 50.8, 6.91, 50.81), box(7.1, 51.1, 7.11, 51.11)]
                ),
                Polygon(
                    [
                        (6.8, 50.8),
                        (6.81, 50.81),
                        (6.8, 50.81),
                        (6.81, 50.8),
                        (6.8, 50.8),
                    ]
                ),
            ]
            source.write_text(
                "".join(
                    json.dumps(
                        {
                            "type": "Feature",
                            "properties": {"landuse": "residential", "name": "Köln"},
                            "geometry": mapping(g),
                        }
                    )
                    + "\n"
                    for g in geometries * 4
                )
            )
            snapshots = []
            for workers in (1, 2):
                database = root / f"{workers}.sqlite"
                with (
                    patch.object(indexing, "BATCH_BYTES", 256),
                    patch.object(indexing.log, "info") as info,
                ):
                    cells = indexing.index_polygons(source, database, workers)
                    completed = [
                        c.kwargs
                        for c in info.call_args_list
                        if c.args[0] == "built_up.indexed"
                    ]
                    self.assertEqual(completed[0]["repaired"], 4)
                with closing(sqlite3.connect(database)) as db:
                    rows = db.execute(
                        "SELECT id,geometry FROM polygons ORDER BY id"
                    ).fetchall()
                    bounds = db.execute("SELECT * FROM bounds ORDER BY id").fetchall()
                snapshots.append((cells, rows, bounds))
            self.assertEqual(snapshots[0], snapshots[1])

    def test_empty_and_filtered_inputs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for number, data in enumerate(
                (
                    "",
                    json.dumps(
                        {
                            "type": "Feature",
                            "properties": {"landuse": "forest"},
                            "geometry": mapping(box(6, 50, 7, 51)),
                        }
                    )
                    + "\n",
                )
            ):
                source = root / f"{number}.geojsonl"
                source.write_text(data)
                self.assertEqual(
                    indexing.index_polygons(source, root / f"{number}.sqlite", 2), []
                )

    def test_worker_errors_propagate_instead_of_publishing_a_partial_index(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "bad.geojsonl"
            source.write_text(
                json.dumps(
                    {
                        "type": "Feature",
                        "properties": {"landuse": "residential"},
                        "geometry": mapping(box(181, 50, 182, 51)),
                    }
                )
                + "\n"
            )
            with self.assertRaisesRegex(
                ValueError, "Unsupported built-up geometry bounds"
            ):
                indexing.index_polygons(source, root / "index.sqlite", 2)
