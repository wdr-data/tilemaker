"""A clipped parent tile must not erase valid forest from its children."""

import gzip
import json
import math
import sqlite3
import subprocess
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from shapely.geometry import Point, Polygon

from wdr_tiles import mvt

ROOT = Path(__file__).resolve().parents[1]
TILEMAKER = ROOT / "tilemaker"
FOREST_ID = 1315781


def forest_at(
    database: sqlite3.Connection, zoom: int, lon: float, lat: float
) -> tuple[list[Polygon], Point]:
    fx = (lon + 180) / 360 * 2**zoom
    fy = (1 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2 * 2**zoom
    x, y = int(fx), int(fy)
    row = database.execute(
        "SELECT tile_data FROM tiles "
        "WHERE zoom_level=? AND tile_column=? AND tile_row=?",
        (zoom, x, 2**zoom - 1 - y),
    ).fetchone()
    if row is None:
        raise AssertionError(f"Missing tile {zoom}/{x}/{y}")
    tile = gzip.decompress(row[0])
    for layer in mvt.fields(tile):
        if layer.tag != 3:
            continue
        fields = list(mvt.fields(layer.payload))
        name = next(field.payload for field in fields if field.tag == 1)
        if name != b"landcover":
            continue
        extent = next(
            (mvt.numbers(field.payload)[0] for field in fields if field.tag == 5),
            4096,
        )
        polygons: list[Polygon] = []
        for feature in (field for field in fields if field.tag == 2):
            attributes = list(mvt.fields(feature.payload))
            feature_id = next(
                (
                    mvt.numbers(field.payload)[0]
                    for field in attributes
                    if field.tag == 1
                ),
                None,
            )
            if feature_id == FOREST_ID:
                geometry = next(field.payload for field in attributes if field.tag == 4)
                polygons.extend(mvt.polygons(geometry))
        return polygons, Point((fx - x) * extent, (fy - y) * extent)
    raise AssertionError(f"Missing landcover layer in {zoom}/{x}/{y}")


@unittest.skipUnless(TILEMAKER.is_file(), "Build the native tilemaker executable first")
class ForestClippingTest(unittest.TestCase):
    def test_forest_and_clearings_survive_parent_clipping(self) -> None:
        # z13+14 exercises the ancestor cache; z14 alone tests direct clipping.
        # Both worker counts matter because the cache is thread-local.
        for minzoom, threads in ((13, 1), (13, 4), (14, 1)):
            with self.subTest(minzoom=minzoom, threads=threads):
                with tempfile.TemporaryDirectory() as directory:
                    temporary = Path(directory)
                    config = json.loads(
                        (ROOT / "resources/config-openmaptiles-nrw.json").read_text()
                    )
                    # Keep production processing, without external coastline data.
                    del config["layers"]["ocean"]
                    config["settings"].update(
                        minzoom=minzoom, maxzoom=14, basezoom=14, include_ids=True
                    )
                    config_path = temporary / "config.json"
                    config_path.write_text(json.dumps(config))
                    output = temporary / "forest.mbtiles"
                    result = subprocess.run(
                        [
                            str(TILEMAKER),
                            "--input",
                            str(ROOT / "test/fixtures/forest-1315781.osm.pbf"),
                            "--output",
                            str(output),
                            "--config",
                            str(config_path),
                            "--process",
                            "resources/process-openmaptiles.lua",
                            "--bbox=6.24,50.49,6.31,50.53",
                            "--threads",
                            str(threads),
                        ],
                        cwd=ROOT,
                        capture_output=True,
                        text=True,
                        timeout=60,
                        check=False,
                    )
                    self.assertEqual(
                        result.returncode, 0, result.stdout + result.stderr
                    )
                    with closing(sqlite3.connect(output)) as database:
                        for zoom in range(minzoom, 15):
                            # Reported disappearing forest, plus two genuine holes.
                            for lon, lat, covered in (
                                (6.27645, 50.50824, True),
                                (6.27827, 50.50941, False),
                                (6.28041, 50.50808, False),
                            ):
                                polygons, point = forest_at(database, zoom, lon, lat)
                                self.assertTrue(polygons, f"Forest missing at z{zoom}")
                                for polygon in polygons:
                                    self.assertTrue(
                                        polygon.is_valid, f"Invalid at z{zoom}"
                                    )
                                    # Positive area is clockwise in MVT's downward Y axis.
                                    self.assertTrue(polygon.exterior.is_ccw)
                                    self.assertTrue(
                                        all(
                                            not ring.is_ccw
                                            for ring in polygon.interiors
                                        )
                                    )
                                self.assertEqual(
                                    any(polygon.covers(point) for polygon in polygons),
                                    covered,
                                    f"Wrong coverage at z{zoom}, {lon}/{lat}",
                                )
