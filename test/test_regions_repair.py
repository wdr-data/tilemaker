"""Final integer geometry repair, attribute preservation and rollback."""

import gzip
import sqlite3
import tempfile
import unittest
from pathlib import Path

from wdr_tiles import mvt, regions_repair

FIXTURE = Path(__file__).parent / "fixtures/regions-invalid-z3.pbf.gz"


class RegionsRepairTest(unittest.TestCase):
    def test_encoded_region_repair_preserves_outlines_and_attributes(self) -> None:
        original = FIXTURE.read_bytes()
        result = regions_repair.repair_tile(original)
        self.assertGreater(result.invalid_polygons, 0)
        before = list(mvt.fields(gzip.decompress(original)))
        after = list(mvt.fields(gzip.decompress(result.data)))
        for a, b in zip(before, after, strict=True):
            if a.raw == b.raw:
                continue
            self.assertEqual(a.tag, 3)
            fields = list(mvt.fields(a.payload))
            self.assertEqual(next(f.payload for f in fields if f.tag == 1), b"regions")
            for left, right in zip(fields, mvt.fields(b.payload), strict=True):
                if left.tag != 2:
                    self.assertEqual(left.raw, right.raw)
                    continue
                lf, rf = list(mvt.fields(left.payload)), list(mvt.fields(right.payload))
                self.assertEqual(
                    [f.raw for f in lf if f.tag != 4], [f.raw for f in rf if f.tag != 4]
                )
                for geom in (f.payload for f in rf if f.tag == 4):
                    self.assertTrue(all(p.is_valid for p in mvt.polygons(geom)))
        again = regions_repair.repair_tile(result.data)
        self.assertEqual(again.invalid_polygons, 0)
        self.assertEqual(again.data, result.data)

    def test_database_repair_rolls_back_if_a_later_batch_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "regions.mbtiles"
            original = FIXTURE.read_bytes()
            rows = [(14, x, 1, original) for x in range(128)] + [(14, 128, 1, b"\x80")]
            with sqlite3.connect(path) as db:
                db.execute(
                    "CREATE TABLE tiles(zoom_level INTEGER,tile_column INTEGER,tile_row INTEGER,tile_data BLOB)"
                )
                db.executemany("INSERT INTO tiles VALUES (?,?,?,?)", rows)
            with self.assertRaisesRegex(ValueError, "14/128/16382"):
                regions_repair.repair(path, workers=1)
            with sqlite3.connect(path) as db:
                self.assertEqual(
                    db.execute("SELECT * FROM tiles ORDER BY tile_column").fetchall(),
                    rows,
                )
                db.execute("DELETE FROM tiles WHERE tile_column=128")
            result = regions_repair.repair(path, workers=2)
            self.assertEqual(result.tiles_checked, 128)
            self.assertEqual(result.tiles_changed, 128)
