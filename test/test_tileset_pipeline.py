import hashlib
import os
import sqlite3
import tempfile
import unittest
from collections.abc import Sequence
from contextlib import closing
from pathlib import Path
from unittest.mock import Mock, patch

from click.testing import CliRunner

from wdr_tiles import mbtiles as disjoint
from wdr_tiles.cli import main
from wdr_tiles.pipeline import Pipeline
from wdr_tiles.settings import Settings
from wdr_tiles.setup import download
from wdr_tiles.state import CommandArg, publish, reusable


def make_tiles(
    path: Path, rows: Sequence[tuple[int, int, int, bytes]], as_view: bool = False
) -> None:
    with closing(sqlite3.connect(path)) as db, db:
        db.execute("CREATE TABLE metadata (name TEXT, value TEXT)")
        db.execute("INSERT INTO metadata VALUES ('name', 'fixture')")
        name = "backing" if as_view else "tiles"
        db.execute(f"""CREATE TABLE {name} (zoom_level INTEGER, tile_column INTEGER,
            tile_row INTEGER, tile_data BLOB, PRIMARY KEY (zoom_level,tile_column,tile_row))""")
        db.executemany(f"INSERT INTO {name} VALUES (?,?,?,?)", rows)
        if as_view:
            db.execute("CREATE VIEW tiles AS SELECT * FROM backing")


class PriorityMergeTest(unittest.TestCase):
    def test_replacement_keeps_high_detail_and_coverage_without_duplicates(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            inputs = [root / f"{name}.mbtiles" for name in ["europe", "dach", "nrw"]]
            make_tiles(inputs[0], [(12, 1, 1, b"europe"), (13, 3, 3, b"broad")])
            make_tiles(
                inputs[1],
                [(13, 3, 3, b"dach-overlap"), (13, 4, 3, b"dach-outside")],
                as_view=True,
            )
            make_tiles(inputs[2], [(13, 3, 3, b"nrw"), (14, 6, 6, b"nrw-detail")])
            hashes = [hashlib.sha256(path.read_bytes()).hexdigest() for path in inputs]
            outputs = disjoint.disjoint(inputs, root / "filtered")
            tiles = {}
            for path in outputs:
                with closing(sqlite3.connect(path)) as db, db:
                    for z, x, y, data in db.execute("SELECT * FROM tiles"):
                        self.assertNotIn((z, x, y), tiles)
                        tiles[z, x, y] = data
                    self.assertEqual(
                        db.execute(
                            'SELECT value FROM metadata WHERE name="name"'
                        ).fetchone()[0],
                        "fixture",
                    )
            self.assertEqual(
                tiles,
                {
                    (12, 1, 1): b"europe",
                    (13, 3, 3): b"nrw",
                    (13, 4, 3): b"dach-outside",
                    (14, 6, 6): b"nrw-detail",
                },
            )
            self.assertEqual(
                hashes,
                [hashlib.sha256(path.read_bytes()).hexdigest() for path in inputs],
            )
            self.assertEqual(outputs[-1], inputs[-1])

    def test_nonoverlapping_inputs_are_reused(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            a, b = root / "a.mbtiles", root / "b.mbtiles"
            make_tiles(a, [(12, 1, 1, b"a")])
            make_tiles(b, [(13, 2, 2, b"b")])
            self.assertEqual(disjoint.disjoint([a, b], root / "filtered"), [a, b])


class ConfigurationTest(unittest.TestCase):
    def test_env_precedence_relative_paths_and_boolean_validation(self) -> None:
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.dict(os.environ, {}, clear=True),
        ):
            root = Path(tmp)
            (root / ".env").write_text(
                "THREADS=64\nFAST=true\nEUROPE_INPUT=data/europe.osm.pbf\n"
            )
            with patch.dict(os.environ, {"THREADS": "4"}):
                settings = Settings.load(root)
            self.assertEqual(settings.threads, 4)
            self.assertTrue(settings.fast)
            self.assertEqual(settings.europe, root / "data/europe.osm.pbf")
            with patch.dict(os.environ, {"FAST": "maybe"}):
                with self.assertRaisesRegex(ValueError, "FAST"):
                    Settings.load(root)

    def test_empty_shell_values_override_env_file_and_keep_path_defaults(self) -> None:
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.dict(os.environ, {}, clear=True),
        ):
            root = Path(tmp)
            (root / ".env").write_text(
                "GEOFABRIK_DATE=260910\nSTORE_DIR=store\nFAST=true\nTHREADS=16\nEUROPE_INPUT=\n"
            )
            with patch.dict(
                os.environ, {"GEOFABRIK_DATE": "", "STORE_DIR": "", "FAST": "false"}
            ):
                settings = Settings.load(root)
            self.assertEqual(settings.geofabrik_date, "")
            self.assertIsNone(settings.store)
            self.assertFalse(settings.fast)
            self.assertEqual(settings.threads, 16)
            self.assertEqual(
                settings.europe, root / "osm/europe-260910-renumbered.osm.pbf"
            )

    def test_invalid_settings_fail_before_creating_output(self) -> None:
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.dict(os.environ, {}, clear=True),
        ):
            root = Path(tmp)
            for key, value in (("THREADS", "0"), ("EUROPE_SHA256", "not-a-checksum")):
                with patch.dict(os.environ, {key: value}):
                    result = CliRunner().invoke(
                        main, ["--project-dir", str(root), "run"]
                    )
                self.assertNotEqual(result.exit_code, 0)
                self.assertIn(key, result.output)
                self.assertFalse((root / "tilesets").exists())

    def test_plan_does_not_create_outputs(self) -> None:
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.dict(os.environ, {}, clear=True),
        ):
            root = Path(tmp)
            result = CliRunner().invoke(main, ["--project-dir", str(root), "plan"])
            self.assertEqual(result.exit_code, 0, result.output)
            self.assertIn(
                "coastline → built-up masks → europe → dach → nrw → merge",
                result.output,
            )
            self.assertFalse((root / "tilesets").exists())

    def test_run_orders_dependencies_before_build_and_merge(self) -> None:
        pipeline = Pipeline(Mock(spec=Settings))
        events = []
        with (
            patch.object(pipeline, "setup", side_effect=lambda: events.append("setup")),
            patch.object(
                pipeline, "download", side_effect=lambda: events.append("download")
            ),
            patch.object(
                pipeline, "extract_nrw", side_effect=lambda: events.append("extract")
            ),
            patch.object(pipeline, "build", side_effect=events.append),
            patch.object(pipeline, "merge", side_effect=lambda: events.append("merge")),
        ):
            pipeline.run()
        self.assertEqual(
            events,
            [
                "setup",
                "download",
                "extract",
                "coastline",
                "europe",
                "dach",
                "nrw",
                "merge",
            ],
        )


class ResumeTest(unittest.TestCase):
    def test_record_is_published_with_mbtiles_and_cannot_overwrite(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            staged, output = root / "staged.mbtiles", root / "output.mbtiles"
            make_tiles(staged, [(6, 1, 1, b"tile")])
            record = {"input": "snapshot-A", "config": "hash-A"}
            publish(staged, output, record)
            self.assertTrue(reusable(output, record))
            with self.assertRaisesRegex(ValueError, "build record"):
                reusable(output, {**record, "config": "hash-B"})
            # Another publication must never alter the destination's embedded record.
            other = root / "other.mbtiles"
            make_tiles(other, [(6, 1, 1, b"other")])
            with self.assertRaises(FileExistsError):
                publish(other, output, {"input": "snapshot-B"})
            self.assertTrue(reusable(output, record))

    def test_modified_extracted_pbf_is_not_reused(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            staged, output = root / "staged.pbf", root / "output.pbf"
            staged.write_bytes(b"original")
            publish(staged, output, {"step": "extract"})
            self.assertTrue(reusable(output, {"step": "extract"}))
            output.write_bytes(b"changed input")
            with self.assertRaisesRegex(ValueError, "build record"):
                reusable(output, {"step": "extract"})

    def test_failed_native_build_does_not_publish_output(self) -> None:
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.dict(os.environ, {}, clear=True),
        ):
            settings = Settings.load(Path(tmp))
            pipeline = Pipeline(settings)
            with (
                patch.object(pipeline, "build_record", return_value={"bbox": None}),
                patch(
                    "wdr_tiles.pipeline.run_command",
                    side_effect=ValueError("native failure"),
                ),
            ):
                with self.assertRaisesRegex(ValueError, "native failure"):
                    pipeline.build("dach")
            self.assertFalse(settings.output("dach").exists())
            self.assertEqual(list(settings.output_dir.iterdir()), [])

    def test_changed_source_during_build_is_not_published(self) -> None:
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.dict(os.environ, {}, clear=True),
        ):
            settings = Settings.load(Path(tmp))
            pipeline = Pipeline(settings)
            with (
                patch.object(
                    pipeline,
                    "build_record",
                    side_effect=[{"bbox": None}, {"bbox": "changed"}],
                ),
                patch("wdr_tiles.pipeline.run_command"),
            ):
                with self.assertRaisesRegex(ValueError, "changed during"):
                    pipeline.build("dach")
            self.assertFalse(settings.output("dach").exists())


class DownloadTest(unittest.TestCase):
    def test_bad_checksum_keeps_partial_and_does_not_publish(self) -> None:
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.dict(os.environ, {}, clear=True),
        ):
            root = Path(tmp)
            destination = root / "europe.osm.pbf"

            def curl(
                settings: Settings, label: str, args: Sequence[CommandArg]
            ) -> None:
                Path(str(destination) + ".part").write_bytes(b"bad download")

            with patch("wdr_tiles.setup.run_command", side_effect=curl):
                with self.assertRaisesRegex(ValueError, "SHA-256 mismatch"):
                    download(
                        Settings.load(root),
                        destination,
                        "https://example.com/data",
                        "0" * 64,
                    )
            self.assertFalse(destination.exists())
            self.assertTrue(Path(str(destination) + ".part").exists())


class NativeSetupTest(unittest.TestCase):
    def test_fresh_host_installs_dependencies_and_builds_pinned_tools(self) -> None:

        from wdr_tiles.setup import TIPPECANOE_REVISION, native_tools

        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.dict(os.environ, {}, clear=True),
        ):
            root = Path(tmp)
            settings = Settings.load(root).model_copy(
                update={
                    "install_system_packages": True,
                    "tilemaker": str(root / "tilemaker"),
                    "tile_join": str(root / ".tools/tippecanoe/tile-join"),
                    "tile_decode": str(root / ".tools/tippecanoe/tippecanoe-decode"),
                    "osmium": "osmium",
                }
            )
            present = {"apt-get", "sudo", "git", "make"}
            commands = []

            def execute(
                settings: Settings, label: str, args: Sequence[CommandArg]
            ) -> None:
                commands.append([str(arg) for arg in args])
                if label == "apt-install":
                    present.update(("osmium", "curl"))
                elif label == "compile-tilemaker":
                    present.add(settings.tilemaker)
                elif label == "compile-tippecanoe":
                    present.update((settings.tile_join, settings.tile_decode))

            with (
                patch(
                    "wdr_tiles.setup.available",
                    side_effect=lambda tool: tool in present,
                ),
                patch("wdr_tiles.setup.run_command", side_effect=execute),
                patch("wdr_tiles.setup.os.geteuid", return_value=1000),
            ):
                native_tools(settings)
            self.assertEqual(commands[0], ["sudo", "-n", "apt-get", "update"])
            self.assertTrue(any(TIPPECANOE_REVISION in args for args in commands))
            self.assertTrue(
                any(
                    args[-2:] == ["tile-join", "tippecanoe-decode"] for args in commands
                )
            )

    def test_complete_shape_download_ignores_archive_paths(self) -> None:
        from zipfile import ZipFile

        from wdr_tiles.setup import SHAPE_EXTENSIONS, static_data

        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.dict(os.environ, {}, clear=True),
        ):
            root = Path(tmp)
            archive = root / ".tools/downloads/shapes.zip"
            archive.parent.mkdir(parents=True)
            with ZipFile(archive, "w") as zipfile:
                for extension in SHAPE_EXTENSIONS:
                    zipfile.writestr("nested/water" + extension, b"component")
                zipfile.writestr("../../unwanted.txt", b"not extracted")
            with patch(
                "wdr_tiles.setup.SHAPEFILES",
                [("coastline/water", "https://example.com/shapes.zip")],
            ):
                static_data(Settings.load(root))
            for extension in SHAPE_EXTENSIONS:
                self.assertEqual(
                    (root / ("coastline/water" + extension)).read_bytes(), b"component"
                )
            self.assertFalse((root / "unwanted.txt").exists())


if __name__ == "__main__":
    unittest.main()
