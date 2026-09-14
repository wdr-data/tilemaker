"""Topology, region identifiers and fill/outline agreement before tile encoding."""

import json
import tempfile
import unittest
from pathlib import Path

from shapely.geometry import MultiPolygon, Point, Polygon, box, mapping, shape

from wdr_tiles.regions_geometry import (
    STATE_CODES,
    Region,
    mask_collection,
    read_regions,
    write_zoom_sources,
)


class RegionsTest(unittest.TestCase):
    def test_masks_preserve_islands_and_holes(self) -> None:
        outer = Polygon(
            [(6, 50), (8, 50), (8, 52), (6, 52), (6, 50)],
            [[(6.5, 50.5), (7, 50.5), (7, 51), (6.5, 51), (6.5, 50.5)]],
        )
        region = Region(
            "state", "DE-NW", "NRW", MultiPolygon([outer, box(9, 50, 9.1, 50.1)])
        )
        collection = mask_collection(region, 14)
        features = collection["features"]
        assert isinstance(features, list)
        inverse = shape(features[0]["geometry"])
        self.assertTrue(inverse.is_valid)
        self.assertFalse(inverse.covers(Point(6.2, 50.2)))
        self.assertFalse(inverse.covers(Point(9.05, 50.05)))
        self.assertTrue(inverse.covers(Point(6.7, 50.7)))
        self.assertTrue(inverse.covers(Point(10, 50)))
        self.assertEqual(features[1]["properties"]["role"], "outline")

    def test_fill_and_outline_use_identical_zoom_geometry(self) -> None:
        regions = [
            Region("country", "DE", "Germany", box(6, 50, 8, 52)),
            Region("state", "DE-NW", "NRW", box(6.1, 50.1, 7.9, 51.9)),
            Region("postal", "01067", "01067", box(7, 51, 7.1, 51.1)),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            layers = write_zoom_sources(regions, root, 7)
            self.assertIn("region_boundaries", layers)
            for zoom in range(8):
                fills = [
                    json.loads(line)
                    for line in (root / f"fill-z{zoom}.geojsonl")
                    .read_text()
                    .splitlines()
                ]
                outlines = [
                    json.loads(line)
                    for line in (root / f"outline-z{zoom}.geojsonl")
                    .read_text()
                    .splitlines()
                ]
                expected = 1 if zoom < 3 else 2 if zoom < 7 else 3
                self.assertEqual(len(fills), expected)
                for fill, outline in zip(fills, outlines, strict=True):
                    self.assertEqual(fill["properties"], outline["properties"])
                    self.assertTrue(
                        shape(fill["geometry"]).boundary.equals(
                            shape(outline["geometry"])
                        )
                    )
                if zoom == 7:
                    self.assertEqual(fills[-1]["properties"]["id"], "postal:01067")
                    self.assertEqual(fills[-1]["properties"]["code"], "01067")

    def test_selection_requires_complete_states_and_preserves_postcode_strings(
        self,
    ) -> None:
        def feature(
            tags: dict[str, str], bounds: tuple[float, float, float, float]
        ) -> str:
            return json.dumps(
                {
                    "type": "Feature",
                    "properties": tags,
                    "geometry": mapping(box(*bounds)),
                }
            )

        lines = [
            feature(
                {
                    "boundary": "administrative",
                    "admin_level": "2",
                    "ISO3166-1:alpha2": "DE",
                },
                (5, 47, 15, 55),
            )
        ]
        lines.extend(
            feature(
                {
                    "boundary": "administrative",
                    "admin_level": "4",
                    "ISO3166-2": "DE-" + code,
                },
                (6, 50, 7, 51),
            )
            for code in sorted(STATE_CODES)
        )
        lines += [
            feature(
                {"boundary": "postal_code", "postal_code": "01067"}, (7, 50, 7.1, 50.1)
            ),
            feature(
                {"boundary": "postal_code", "postal_code": "99999"}, (20, 50, 21, 51)
            ),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "regions.geojsonl"
            path.write_text("\n".join(lines))
            regions = read_regions(path)
            self.assertEqual([r.code for r in regions if r.kind == "postal"], ["01067"])
            path.write_text("\n".join(lines[1:]))
            with self.assertRaisesRegex(ValueError, "Germany and all 16"):
                read_regions(path)
