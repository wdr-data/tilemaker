"""Encoded geometry regressions, repair scope, and atomic publication."""

import gzip
import json
import sqlite3
import tempfile
import unittest
from collections.abc import Sequence
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

import shapely
from shapely.geometry import Polygon, box

from wdr_tiles import mvt, tile_repair
from wdr_tiles.pipeline import Pipeline
from wdr_tiles.settings import Settings
from wdr_tiles.state import CommandArg, reusable

FIXTURE = Path(__file__).parent / "fixtures/built-up-invalid-7-68-43.pbf.gz"


def make_database(path: Path, rows: list[tuple[int, int, int, bytes]]) -> None:
    with closing(sqlite3.connect(path)) as db, db:
        db.execute("CREATE TABLE metadata(name TEXT,value TEXT)")
        db.execute("INSERT INTO metadata VALUES ('name','regression')")
        db.execute(
            "CREATE TABLE tiles(zoom_level INTEGER,tile_column INTEGER,tile_row INTEGER,tile_data BLOB,PRIMARY KEY(zoom_level,tile_column,tile_row))"
        )
        db.executemany("INSERT INTO tiles VALUES (?,?,?,?)", rows)


def layers(data: bytes) -> dict[bytes, bytes]:
    return {
        next(f.payload for f in mvt.fields(field.payload) if f.tag == 1): field.payload
        for field in mvt.fields(data)
        if field.tag == 3
    }


def built_up_geometries(layer: bytes) -> list[bytes]:
    fields = list(mvt.fields(layer))
    keys = [f.payload.decode() for f in fields if f.tag == 3]
    values = [
        next((v.payload.decode() for v in mvt.fields(f.payload) if v.tag == 1), None)
        for f in fields
        if f.tag == 4
    ]
    result = []
    for field in fields:
        if field.tag != 2:
            continue
        feature = list(mvt.fields(field.payload))
        tags = [v for f in feature if f.tag == 2 for v in mvt.numbers(f.payload)]
        if any(
            keys[k] == "class" and values[v] == "built_up"
            for k, v in zip(tags[::2], tags[1::2], strict=True)
        ):
            result.extend(f.payload for f in feature if f.tag == 4)
    return result


def raw_geometry(polygons: Sequence[Polygon]) -> bytes:
    output = bytearray()
    x = y = 0
    for polygon in polygons:
        for ring in (polygon.exterior, *polygon.interiors):
            points = list(ring.coords)[:-1]
            for i, (px, py) in enumerate(points):
                if i == 0:
                    output += mvt.varint(9)
                elif i == 1:
                    output += mvt.varint((len(points) - 1) * 8 + 2)
                for delta in (int(px) - x, int(py) - y):
                    output += mvt.varint(delta * 2 if delta >= 0 else -delta * 2 - 1)
                x, y = int(px), int(py)
            output += mvt.varint(15)
    return bytes(output)


class EncodedGeometryTest(unittest.TestCase):
    def test_real_slow_tile_becomes_valid_without_touching_other_data(self) -> None:
        original = gzip.decompress(FIXTURE.read_bytes())
        result = tile_repair.repair_tile(original)
        before, after = layers(original), layers(result.data)
        self.assertGreater(result.invalid_polygons, 0)
        self.assertEqual(set(before), set(after))
        for name in before:
            if name != b"landuse":
                self.assertEqual(before[name], after[name])
        # All layer dictionaries, feature IDs, tag indices, unknown fields and
        # non-target geometries survive. Compare feature geometry differences
        # against the actual class tags instead of just comparing feature counts.
        original_fields = list(mvt.fields(before[b"landuse"]))
        repaired_fields = list(mvt.fields(after[b"landuse"]))
        targets = built_up_geometries(before[b"landuse"])
        self.assertEqual(len(original_fields), len(repaired_fields))
        changed = 0
        for left, right in zip(original_fields, repaired_fields, strict=True):
            if left.tag != 2 or left.raw == right.raw:
                self.assertEqual(left.raw, right.raw)
                continue
            changed += 1
            a, b = list(mvt.fields(left.payload)), list(mvt.fields(right.payload))
            self.assertEqual(
                [f.raw for f in a if f.tag != 4], [f.raw for f in b if f.tag != 4]
            )
            self.assertTrue(all(f.payload in targets for f in a if f.tag == 4))
        self.assertGreater(changed, 0)
        for data in built_up_geometries(after[b"landuse"]):
            for polygon in mvt.polygons(data):
                self.assertTrue(polygon.is_valid)
        self.assertEqual(
            tile_repair.repair_tile(result.data),
            tile_repair.RepairResult(result.data, 0),
        )
        zipped = tile_repair.repair_tile(FIXTURE.read_bytes())
        self.assertEqual(gzip.decompress(zipped.data), result.data)

    def test_integer_repair_preserves_holes_and_removes_collapsed_spur(self) -> None:
        exterior = [
            (0, 0),
            (20, 0),
            (20, 20),
            (10, 20),
            (10, 25),
            (10, 20),
            (0, 20),
            (0, 0),
        ]
        original = Polygon(exterior, [[(5, 5), (5, 15), (15, 15), (15, 5), (5, 5)]])
        self.assertFalse(original.is_valid)
        expected = box(0, 0, 20, 20).difference(box(5, 5, 15, 15))
        result = tile_repair.repair_geometry(raw_geometry([original]))
        actual = mvt.polygons(result.data)
        self.assertEqual(result.invalid_polygons, 1)
        self.assertEqual(len(actual), 1)
        self.assertTrue(actual[0].equals(expected))
        self.assertEqual(len(actual[0].interiors), 1)

    def test_opposite_winding_buffer_coordinates_and_multiple_polygons(self) -> None:
        original = [
            box(-20, -10, 10, 10).difference(box(-5, -5, 5, 5)),
            box(4100, 0, 4120, 20),
        ]
        encoded = mvt.encode_polygons(original)
        rings = list(mvt.rings(encoded))
        # Build intentionally reversed winding using the same raw MVT commands.
        reversed_polygons = [
            Polygon(
                list(reversed(p.exterior.coords)),
                [list(reversed(h.coords)) for h in p.interiors],
            )
            for p in map(shapely.orient_polygons, original)
        ]
        self.assertEqual(len(rings), 3)

        for geometry in (encoded, raw_geometry(reversed_polygons)):
            decoded = mvt.polygons(geometry)
            self.assertEqual(len(decoded), 2)
            for a, b in zip(original, decoded, strict=True):
                self.assertTrue(a.equals(b))

    def test_malformed_commands_fail_instead_of_silently_dropping_geometry(
        self,
    ) -> None:
        for value in (b"\x80", b"\x09\x00", b"\x12\x00\x00", b"\x09\x00\x00"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                list(mvt.rings(value))
        with self.assertRaises(ValueError):
            list(mvt.fields(b"\x1a\x04a"))


class OverviewDatabaseTest(unittest.TestCase):
    def test_parallel_repair_only_touches_overview_tiles(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "tiles.mbtiles"
            tile = FIXTURE.read_bytes()
            rows = [
                (7, 68, 84, tile),
                (8, 136, 168, tile),
                (5, 1, 1, b"outside"),
                (10, 1, 1, b"outside"),
            ]
            make_database(path, rows)
            stats = tile_repair.repair_overviews(path, workers=2)
            self.assertEqual(stats.tiles_checked, 2)
            self.assertEqual(stats.tiles_changed, 2)
            with closing(sqlite3.connect(path)) as db:
                saved = db.execute("SELECT * FROM tiles ORDER BY zoom_level").fetchall()
                self.assertEqual(saved[0][3], b"outside")
                self.assertEqual(saved[-1][3], b"outside")
                self.assertEqual(saved[1][3], saved[2][3])
                self.assertNotEqual(saved[1][3], tile)
                self.assertEqual(
                    db.execute(
                        "SELECT value FROM metadata WHERE name='name'"
                    ).fetchone()[0],
                    "regression",
                )
            again = tile_repair.repair_overviews(path)
            self.assertEqual(again.tiles_changed, 0)

    def test_failure_rolls_back_earlier_repairs_and_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "tiles.mbtiles"
            rows = [(7, 1, 1, FIXTURE.read_bytes()), (7, 2, 1, b"\x80")]
            make_database(path, rows)
            with self.assertRaisesRegex(ValueError, "7/2/126"):
                tile_repair.repair_overviews(path)
            with closing(sqlite3.connect(path)) as db:
                self.assertEqual(
                    db.execute("SELECT * FROM tiles ORDER BY tile_column").fetchall(),
                    rows,
                )
                self.assertEqual(
                    db.execute("SELECT * FROM metadata").fetchall(),
                    [("name", "regression")],
                )


class BuildIntegrationTest(unittest.TestCase):
    def test_europe_repaired_before_publish_and_failed_repair_not_published(
        self,
    ) -> None:
        for fail in (False, True):
            with self.subTest(fail=fail), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                settings = Settings(root=root, built_up_workers=1)
                settings.config("europe").parent.mkdir(parents=True)
                settings.config("europe").write_text(
                    json.dumps({"layers": {}, "settings": {}})
                )
                pipeline = Pipeline(settings)

                def native(
                    settings: Settings,
                    label: str,
                    args: Sequence[CommandArg],
                    should_fail: bool = fail,
                ) -> None:
                    path = Path(str(args[list(args).index("--output") + 1]))
                    make_database(
                        path,
                        [(7, 68, 84, b"\x80" if should_fail else FIXTURE.read_bytes())],
                    )

                with (
                    patch.object(pipeline, "prepare_built_up"),
                    patch.object(pipeline, "build_record", return_value={"bbox": None}),
                    patch("wdr_tiles.pipeline.built_up.validate", return_value={}),
                    patch("wdr_tiles.pipeline.built_up.add_layers"),
                    patch("wdr_tiles.pipeline.built_up.build_record", return_value={}),
                    patch("wdr_tiles.pipeline.run_command", side_effect=native),
                ):
                    if fail:
                        with self.assertRaises(ValueError):
                            pipeline.build("europe")
                        self.assertFalse(settings.output("europe").exists())
                    else:
                        pipeline.build("europe")
                        self.assertTrue(
                            reusable(settings.output("europe"), {"bbox": None})
                        )
                        with closing(sqlite3.connect(settings.output("europe"))) as db:
                            data = db.execute("SELECT tile_data FROM tiles").fetchone()[
                                0
                            ]
                        self.assertEqual(
                            tile_repair.repair_tile(data).invalid_polygons, 0
                        )
                        self.assertNotEqual(data, FIXTURE.read_bytes())


class PreviewAndRecordTest(unittest.TestCase):
    def test_preview_repairs_before_rendering_comparison(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = Settings(root=root, built_up_workers=1)
            settings.config("europe").parent.mkdir(parents=True)
            settings.config("europe").write_text(
                json.dumps({"layers": {"landuse": {}}, "settings": {}})
            )
            directory = root / "preview"

            def native(
                settings: Settings, label: str, args: Sequence[CommandArg]
            ) -> None:
                if label == "preview-build":
                    make_database(
                        directory / "after.mbtiles", [(7, 68, 84, FIXTURE.read_bytes())]
                    )

            def render(*args: object) -> None:
                with closing(sqlite3.connect(directory / "after.mbtiles")) as db:
                    data = db.execute("SELECT tile_data FROM tiles").fetchone()[0]
                self.assertNotEqual(data, FIXTURE.read_bytes())
                self.assertEqual(tile_repair.repair_tile(data).invalid_polygons, 0)

            with (
                patch("wdr_tiles.pipeline.run_command", side_effect=native),
                patch(
                    "wdr_tiles.pipeline.built_up.prepare", return_value={"sources": []}
                ),
                patch(
                    "wdr_tiles.pipeline.preview.render", side_effect=render
                ) as comparison,
            ):
                Pipeline(settings).preview(
                    root / "before.mbtiles", directory, "6,50,7,51"
                )
                comparison.assert_called_once()

    def test_repair_fingerprint_only_changes_europe_record(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = Settings(root=root)
            pipeline = Pipeline(settings)
            for region in ("europe", "dach"):
                settings.config(region).parent.mkdir(parents=True, exist_ok=True)
                settings.config(region).write_text(
                    json.dumps({"layers": {}, "settings": {}})
                )
                settings.process(region).write_text("-- fixture")
                settings.input(region).parent.mkdir(parents=True, exist_ok=True)
                settings.input(region).write_bytes(b"source")
            directory = settings.output_dir / "built-up"
            directory.mkdir(parents=True)
            (directory / "manifest.json").write_text("{}")
            with (
                patch("wdr_tiles.pipeline.executable_record", return_value="native"),
                patch("wdr_tiles.pipeline.built_up.build_record", return_value={}),
                patch(
                    "wdr_tiles.pipeline.built_up.validate", return_value={"files": []}
                ),
                patch(
                    "wdr_tiles.pipeline.tile_repair.build_record",
                    return_value={"version": 1},
                ) as repair_record,
            ):
                europe = pipeline.build_record("europe")
                dach = pipeline.build_record("dach")
                repair_record.return_value = {"version": 2}
                self.assertNotEqual(europe, pipeline.build_record("europe"))
                self.assertEqual(dach, pipeline.build_record("dach"))
