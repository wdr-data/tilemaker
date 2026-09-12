"""Geometry, chunk seams and publication checks for the overview masks."""

import json
import os
import tempfile
import unittest
from collections.abc import Sequence
from pathlib import Path
from unittest.mock import patch

import shapely
from shapely.geometry import box, mapping

from wdr_tiles import closing
from wdr_tiles import closing_geometry as geometry
from wdr_tiles.settings import Settings
from wdr_tiles.state import CommandArg


class GeometryTest(unittest.TestCase):
    def test_closing_joins_nearby_parcels_but_preserves_wide_gaps(self) -> None:
        parcels = shapely.union_all([box(0, 0, 200, 400), box(280, 0, 480, 400)])
        self.assertEqual(len(list(geometry.polygons(geometry.close(parcels, 25)))), 2)
        self.assertEqual(len(list(geometry.polygons(geometry.close(parcels, 50)))), 1)
        self.assertEqual(geometry.RADII, {6: 100, 7: 75, 8: 50, 9: 25})

    def test_chunk_seams_match_processing_the_same_area_together(self) -> None:
        # Both a polygon crossing the boundary and a gap straddling it.
        parcels = [
            box(6.99, 50.99, 7.01, 51.001),
            box(6.996, 51.002, 6.9997, 51.01),
            box(7.0003, 51.002, 7.004, 51.01),
        ]
        whole = geometry.cell_masks(parcels, (6.5, 50.5, 7.5, 51.5))
        chunks = [
            geometry.cell_masks(parcels, geometry.cell_bounds(cell))
            for cell in [(13, 101), (14, 101), (13, 102), (14, 102)]
        ]
        for zoom in geometry.RADII:
            combined = shapely.union_all(
                [shapely.from_wkb(chunk[zoom]) for chunk in chunks]
            )
            expected = shapely.from_wkb(whole[zoom])
            # Compare in the shared geographic coordinates. Reprojecting a
            # nearly degenerate difference polygon creates artificial wedges
            # because clipping adds vertices along previously straight edges.
            self.assertLess(combined.symmetric_difference(expected).area, 1e-12)
            self.assertLess(combined.hausdorff_distance(expected), 1e-10)

    def test_selection_respects_tag_precedence_and_water_exclusions(self) -> None:
        self.assertEqual(geometry.selected_class({"amenity": "school"}), "school")
        self.assertEqual(
            geometry.selected_class({"landuse": "industrial", "amenity": "school"}),
            "industrial",
        )
        for tags in [
            {"landuse": "grass", "amenity": "school"},
            {"landuse": "residential", "natural": "water"},
            {"landuse": "industrial", "waterway": "dock"},
            {"landuse": "residential", "disused": "yes"},
            {"landuse": "residential", "highway": "proposed"},
            {"landuse": "cemetery"},
        ]:
            self.assertIsNone(geometry.selected_class(tags), tags)

    def test_index_includes_neighbour_cells_for_gap_filling(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source.geojsonl"
            source.write_text(
                json.dumps(
                    {
                        "type": "Feature",
                        "properties": {"landuse": "residential"},
                        "geometry": mapping(box(6.998, 50.7, 6.9999, 50.701)),
                    }
                )
                + "\n"
            )
            cells = closing.index_polygons(source, root / "index.sqlite")
            self.assertIn((13, 101), cells)
            self.assertIn((14, 101), cells)


class PreparationTest(unittest.TestCase):
    def test_real_workers_publish_reusable_masks_and_reject_modified_files(
        self,
    ) -> None:
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.dict(os.environ, {}, clear=True),
        ):
            root = Path(tmp)
            settings = Settings(root=root, built_up_workers=2)
            source, destination = root / "input.osm.pbf", root / "built-up"
            source.write_bytes(b"fixture")

            def export(
                settings: Settings, label: str, args: Sequence[CommandArg]
            ) -> None:
                output = Path(str(args[list(args).index("--output") + 1]))
                if label == "built-up-filter":
                    output.write_bytes(b"fixture")
                else:
                    output.write_text(
                        json.dumps(
                            {
                                "type": "Feature",
                                "properties": {"landuse": "residential"},
                                "geometry": mapping(box(6.999, 50.999, 7.002, 51.002)),
                            }
                        )
                        + "\n"
                    )

            with (
                patch("wdr_tiles.closing.run_command", side_effect=export),
                patch(
                    "wdr_tiles.closing.build_record", return_value={"input": "fixture"}
                ),
            ):
                manifest = closing.prepare(settings, source, destination)
                self.assertEqual({s["zoom"] for s in manifest["sources"]}, {6, 7, 8, 9})
                self.assertEqual(
                    closing.prepare(settings, source, destination), manifest
                )
                self.assertEqual(len(list(destination.iterdir())), 5)
                with self.assertRaisesRegex(ValueError, "Stale"):
                    closing.validate(destination, {"input": "changed"})
                (destination / manifest["sources"][0]["filename"]).write_text("changed")
                with self.assertRaisesRegex(ValueError, "Stale"):
                    closing.prepare(settings, source, destination)

    def test_failure_does_not_publish_partial_masks(self) -> None:
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.dict(os.environ, {}, clear=True),
        ):
            root = Path(tmp)
            with (
                patch("wdr_tiles.closing.build_record", return_value={}),
                patch(
                    "wdr_tiles.closing.run_command",
                    side_effect=ValueError("export failed"),
                ),
            ):
                with self.assertRaisesRegex(ValueError, "export failed"):
                    closing.prepare(
                        Settings(root=root), root / "input", root / "built-up"
                    )
            self.assertEqual(list(root.iterdir()), [])
