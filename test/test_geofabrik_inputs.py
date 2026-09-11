import hashlib
import os
import tempfile
import unittest
from collections.abc import Sequence
from pathlib import Path
from unittest.mock import patch

from click.testing import CliRunner

from wdr_tiles.cli import main
from wdr_tiles.inputs import prepare_geofabrik, source_data
from wdr_tiles.settings import Settings
from wdr_tiles.state import CommandArg


class GeofabrikTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        environment = patch.dict(os.environ, {"GEOFABRIK_DATE": "260910"}, clear=True)
        environment.start()
        self.addCleanup(environment.stop)
        self.settings = Settings.load(self.root)
        self.settings.europe.parent.mkdir(parents=True)
        binary = self.root / "fixture-osmium"
        binary.write_bytes(b"osmium binary fixture")
        for mocked in (
            patch("wdr_tiles.inputs.available", return_value=True),
            patch("wdr_tiles.inputs.shutil.which", return_value=str(binary)),
        ):
            mocked.start()
            self.addCleanup(mocked.stop)
        self.raw = self.settings.europe.parent / "europe-260910.osm.pbf"
        self.raw.write_bytes(b"raw source fixture")
        self.md5 = hashlib.md5(self.raw.read_bytes()).hexdigest()
        Path(str(self.raw) + ".md5").write_text(f"{self.md5}  {self.raw.name}\n")

    def renumber(
        self, settings: Settings, label: str, args: Sequence[CommandArg]
    ) -> None:
        self.assertEqual(args[:3], [settings.osmium, "renumber", self.raw])
        Path(str(args[-1])).write_bytes(b"renumbered fixture")

    def test_date_controls_paths_and_rejects_invalid_dates(self) -> None:
        self.assertEqual(self.settings.europe.name, "europe-260910-renumbered.osm.pbf")
        self.assertEqual(self.settings.nrw.name, "nrw-buffer-260910-renumbered.osm.pbf")
        for invalid in ("latest", "260231", "../bad", "26091"):
            with patch.dict(os.environ, {"GEOFABRIK_DATE": invalid}):
                with self.assertRaisesRegex(ValueError, "GEOFABRIK_DATE"):
                    Settings.load(self.root)

    def test_renumber_resume_and_changed_raw_source(self) -> None:
        with patch("wdr_tiles.inputs.run_command", side_effect=self.renumber) as run:
            prepare_geofabrik(self.settings, ("europe",))
            prepare_geofabrik(self.settings, ("europe",))
            self.assertEqual(run.call_count, 1)
            self.raw.write_bytes(b"changed input")
            with self.assertRaisesRegex(ValueError, "build record"):
                prepare_geofabrik(self.settings, ("europe",))
            self.assertEqual(run.call_count, 1)

    def test_checksum_failure_prevents_renumbering(self) -> None:
        self.raw.write_bytes(b"corrupt")
        with patch("wdr_tiles.inputs.run_command") as run:
            with self.assertRaisesRegex(ValueError, "MD5 mismatch"):
                prepare_geofabrik(self.settings, ("europe",))
            run.assert_not_called()
        self.assertFalse(self.settings.europe.exists())

    def test_failed_renumber_keeps_original_without_publishing(self) -> None:
        def fail(settings: Settings, label: str, args: Sequence[CommandArg]) -> None:
            Path(str(args[-1])).write_bytes(b"partial")
            raise ValueError("osmium failed")

        with patch("wdr_tiles.inputs.run_command", side_effect=fail):
            with self.assertRaisesRegex(ValueError, "osmium failed"):
                prepare_geofabrik(self.settings, ("europe",))
        self.assertTrue(self.raw.exists())
        self.assertFalse(self.settings.europe.exists())
        self.assertFalse(list(self.raw.parent.glob(".europe-*")))

    def test_download_uses_dated_geofabrik_urls(self) -> None:
        self.raw.unlink()
        Path(str(self.raw) + ".md5").unlink()

        def fetch(settings: Settings, label: str, args: Sequence[CommandArg]) -> None:
            url = str(args[-1])
            output = Path(str(args[args.index("--output") + 1]))
            expected = "https://download.geofabrik.de/europe-260910.osm.pbf"
            self.assertIn(url, (expected, expected + ".md5"))
            output.write_bytes(
                (self.md5 + "  europe-260910.osm.pbf").encode()
                if url.endswith(".md5")
                else b"raw source fixture"
            )

        with (
            patch("wdr_tiles.setup.run_command", side_effect=fetch) as fetch_command,
            patch("wdr_tiles.inputs.run_command", side_effect=self.renumber),
        ):
            prepare_geofabrik(self.settings, ("europe",))
        self.assertEqual(fetch_command.call_count, 2)

    def test_full_run_prepares_both_geofabrik_regions(self) -> None:
        with (
            patch("wdr_tiles.inputs.prepare_geofabrik") as prepare,
            patch("wdr_tiles.inputs.static_data"),
            patch("wdr_tiles.inputs.run_command"),
        ):
            source_data(self.settings)
        self.assertEqual(
            [call.args[1] for call in prepare.call_args_list], [("europe",), ("dach",)]
        )

    def test_manual_inputs_are_checked_without_downloading(self) -> None:
        self.settings.europe.write_bytes(b"manual europe")
        self.settings.dach.write_bytes(b"manual dach")
        settings = self.settings.model_copy(
            update={
                "geofabrik_date": "",
                "europe_sha256": hashlib.sha256(b"manual europe").hexdigest(),
            }
        )
        with (
            patch("wdr_tiles.inputs.prepare_geofabrik") as prepare,
            patch("wdr_tiles.inputs.download") as download,
            patch("wdr_tiles.inputs.static_data"),
            patch("wdr_tiles.inputs.run_command") as run,
        ):
            source_data(settings)
        prepare.assert_not_called()
        download.assert_not_called()
        self.assertEqual(run.call_count, 2)

    def test_missing_manual_input_explains_how_to_prepare_it(self) -> None:
        settings = self.settings.model_copy(update={"geofabrik_date": ""})
        with patch("wdr_tiles.inputs.download") as download:
            with self.assertRaisesRegex(
                ValueError, "Missing renumbered input.*prepare-inputs"
            ):
                source_data(settings)
        download.assert_not_called()

    def test_manual_input_checksum_mismatch_stops_before_header_check(self) -> None:
        self.settings.europe.write_bytes(b"incorrect input")
        settings = self.settings.model_copy(
            update={"geofabrik_date": "", "europe_sha256": "0" * 64}
        )
        with patch("wdr_tiles.inputs.run_command") as run:
            with self.assertRaisesRegex(ValueError, "SHA-256 mismatch"):
                source_data(settings)
        run.assert_not_called()

    def test_cli_selects_one_region(self) -> None:
        with patch("wdr_tiles.inputs.prepare_geofabrik") as prepare:
            result = CliRunner().invoke(
                main, ["--project-dir", str(self.root), "prepare-inputs", "dach"]
            )
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(prepare.call_args.args[1], ("dach",))


if __name__ == "__main__":
    unittest.main()
