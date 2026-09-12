"""A slow first cell must not prevent later work from reaching idle workers."""

import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from pathlib import Path
from threading import Event
from unittest.mock import patch

import shapely
from shapely.geometry import box, shape

from wdr_tiles import closing
from wdr_tiles.closing_geometry import Cell


class ClosingSchedulerTest(unittest.TestCase):
    def test_refills_past_a_slow_cell_and_preserves_exact_output_order(self) -> None:
        advanced = Event()
        cells: list[Cell] = [(i, 0) for i in range(12)]

        def result(cell: Cell) -> dict[int, bytes]:
            return {6: shapely.to_wkb(box(cell[0], 0, cell[0] + 0.5, 0.5))}

        def process(database: Path, cell: Cell) -> dict[int, bytes]:
            if cell[0] == 0:
                # With two workers the old loop queued just cells 0..3, then
                # blocked on cell 0. Reaching cell 4 proves the queue refilled.
                if not advanced.wait(timeout=5):
                    raise RuntimeError("Worker pool stalled behind the first cell")
            elif cell[0] == 4:
                advanced.set()
            return result(cell)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            actual, expected = root / "actual", root / "expected"
            actual.mkdir()
            expected.mkdir()
            with (
                patch.object(closing, "ProcessPoolExecutor", ThreadPoolExecutor),
                patch.object(closing.geometry, "process_cell", side_effect=process),
            ):
                sources = closing.write_masks(root / "unused.sqlite", cells, actual, 2)
            with ExitStack() as stack:
                writers = closing.MaskWriters(expected, stack)
                for cell in cells:
                    writers.append(result(cell))
            self.assertTrue(advanced.is_set())
            self.assertEqual(sources, [{"zoom": 6, "filename": "built-up-z6.geojsonl"}])
            filename = sources[0]["filename"]
            self.assertEqual(
                (actual / filename).read_bytes(), (expected / filename).read_bytes()
            )
            positions = [
                shape(json.loads(line)["geometry"]).bounds[0]
                for line in (actual / filename).read_text().splitlines()
            ]
            self.assertEqual(positions, list(range(12)))
            self.assertEqual(sorted(p.name for p in actual.iterdir()), [filename])

    def test_worker_failure_identifies_cell_and_cleans_the_spool(self) -> None:
        def fail(database: Path, cell: Cell) -> dict[int, bytes]:
            raise RuntimeError("geometry failed")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with (
                patch.object(closing, "ProcessPoolExecutor", ThreadPoolExecutor),
                patch.object(closing.geometry, "process_cell", side_effect=fail),
            ):
                with self.assertRaisesRegex(
                    ValueError, r"Closing failed in cell \(3, 4\)"
                ) as caught:
                    closing.write_masks(root / "unused.sqlite", [(3, 4)], root, 2)
            self.assertIsInstance(caught.exception.__cause__, RuntimeError)
            self.assertEqual(list(root.iterdir()), [])

    def test_empty_input_creates_no_mask_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.assertEqual(
                closing.write_masks(root / "unused.sqlite", [], root, 2), []
            )
            self.assertEqual(list(root.iterdir()), [])

    def test_failure_is_logged_before_waiting_for_other_workers(self) -> None:
        other_started, failure_logged, observed = Event(), Event(), Event()

        def process(database: Path, cell: Cell) -> dict[int, bytes]:
            if cell[0] == 0:
                if not other_started.wait(timeout=5):
                    raise RuntimeError("Second worker did not start")
                raise RuntimeError("geometry failed")
            other_started.set()
            if failure_logged.wait(timeout=5):
                observed.set()
            return {}

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with (
                patch.object(closing, "ProcessPoolExecutor", ThreadPoolExecutor),
                patch.object(closing.geometry, "process_cell", side_effect=process),
                patch.object(
                    closing.log,
                    "error",
                    side_effect=lambda *args, **kwargs: failure_logged.set(),
                ),
            ):
                with self.assertRaisesRegex(
                    ValueError, r"Closing failed in cell \(0, 0\)"
                ):
                    closing.write_masks(
                        root / "unused.sqlite", [(0, 0), (1, 0)], root, 2
                    )
            self.assertTrue(
                observed.is_set(), "Failure was hidden behind worker shutdown"
            )
            self.assertEqual(list(root.iterdir()), [])
