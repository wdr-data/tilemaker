"""Selection boundaries shared by the overview and native detail export."""

import json
import re
import shutil
import subprocess
import unittest
from pathlib import Path

from wdr_tiles.closing_geometry import PAVED_HIGHWAYS, PAVED_SURFACES, selected_class
from wdr_tiles.indexing import prepare_batch

ROOT = Path(__file__).resolve().parents[1]


class PedestrianSelectionTest(unittest.TestCase):
    def test_both_surface_tags_are_selected_and_non_surface_lines_are_not(self) -> None:
        for tags in (
            {"area:highway": "pedestrian"},
            {"highway": "pedestrian", "area": "yes"},
        ):
            paved = {**tags, "surface": "paving_stones"}
            self.assertEqual(selected_class(paved), "pedestrian")
            self.assertIsNone(selected_class(tags))
            for override in (
                {"surface": "grass"},
                {"surface": "gravel"},
                {"layer": "-1"},
                {"tunnel": "yes"},
                {"covered": "yes"},
                {"landuse": "grass"},
                {"natural": "water"},
                {"landuse": "quarry"},
            ):
                self.assertIsNone(selected_class({**paved, **override}), override)
        self.assertIsNone(
            selected_class({"highway": "pedestrian", "surface": "paving_stones"})
        )

    def test_hard_surfaces_match_lua(self) -> None:
        lua = (ROOT / "resources/process-openmaptiles.lua").read_text()
        match = re.search(r"\bpavedValues\s*= Set\s*\{([^}]+)\}", lua)
        assert match is not None
        self.assertEqual(set(re.findall(r'"([^\"]+)"', match[1])), PAVED_SURFACES)

    def test_exported_relation_provenance_reaches_index(self) -> None:
        # A relation with one outer ring still exports as Polygon, not necessarily
        # MultiPolygon. Geometry type alone cannot identify its OSM provenance.
        feature = {
            "type": "Feature",
            "properties": {
                "@type": "relation",
                "highway": "pedestrian",
                "surface": "sett",
            },
            "geometry": {
                "type": "Polygon",
                "coordinates": [
                    [[7, 51], [7.001, 51], [7.001, 51.001], [7, 51.001], [7, 51]]
                ],
            },
        }
        self.assertEqual(len(prepare_batch([json.dumps(feature).encode()]).rows), 1)
        feature["properties"]["@type"] = "way"
        self.assertEqual(len(prepare_batch([json.dumps(feature).encode()]).rows), 0)

    @unittest.skipUnless(shutil.which("lua"), "Lua interpreter required")
    def test_area_policy_matches_lua_for_positive_and_negative_cases(self) -> None:
        cases: list[tuple[dict[str, str], str | None]] = [
            (
                {"@type": "relation", "highway": "pedestrian", "surface": "sett"},
                "pedestrian",
            ),
            ({"@type": "way", "highway": "pedestrian", "surface": "sett"}, None),
            ({"place": "square", "surface": "paving_stones"}, "square"),
            ({"place": "square", "surface": "paving_stones", "landuse": "grass"}, None),
            ({"place": "square"}, None),
            ({"amenity": "parking", "surface": "asphalt"}, "parking"),
            (
                {"amenity": "parking", "parking": "surface", "surface": "sett"},
                "parking",
            ),
            (
                {"amenity": "parking", "parking": "underground", "surface": "asphalt"},
                None,
            ),
            (
                {"amenity": "parking", "parking": "multi-storey", "surface": "asphalt"},
                None,
            ),
            ({"amenity": "parking", "surface": "grass_paver"}, None),
            ({"amenity": "marketplace", "surface": "bricks"}, "square"),
            ({"amenity": "marketplace", "surface": "bricks", "indoor": "yes"}, None),
            ({"landuse": "garages"}, "garages"),
            ({"landuse": "religious"}, None),
            ({"landuse": "construction"}, None),
            ({"area:highway": "traffic_island", "surface": "paved"}, None),
        ]
        for highway in sorted(PAVED_HIGHWAYS):
            for area in (
                {"area:highway": highway},
                {"highway": highway, "area": "yes"},
                {"highway": highway, "@type": "relation"},
            ):
                paved = {**area, "surface": "paved"}
                cases.append((paved, highway))
                for override in (
                    {"area": "no"},
                    {"layer": "-1"},
                    {"covered": "yes"},
                    {"natural": "water"},
                    {"landuse": "quarry"},
                    {"surface": "gravel"},
                ):
                    cases.append(({**paved, **override}, None))
        script = [
            'dofile("resources/process-openmaptiles.lua")',
            "function IsClosed() return true end",
        ]
        for tags, expected in cases:
            self.assertEqual(selected_class(tags), expected, tags)
            table = ",".join(
                f"[{json.dumps(k)}]={json.dumps(v)}" for k, v in tags.items()
            )
            script.append("do local tags={" + table + "}")
            script.append('function Find(key) return tags[key] or "" end')
            script.append(
                'function IsMultiPolygon() return tags["@type"]=="relation" end'
            )
            literal = "nil" if expected is None else json.dumps(expected)
            script.append(
                f'assert(BuiltUpClass()=={literal}, "case {len(script)}"); end'
            )
        subprocess.run(
            ["lua", "-"], input="\n".join(script), text=True, cwd=ROOT, check=True
        )


class QuarryZoomTest(unittest.TestCase):
    @unittest.skipUnless(shutil.which("lua"), "Lua interpreter required")
    def test_quarries_keep_a_separate_class_and_use_area_thresholds(self) -> None:
        script = r"""
        dofile("resources/process-openmaptiles.lua")
        assert(landuseKeys.quarry and not builtUpLanduseKeys.quarry)
        local result, area
        function Layer(name, polygon) assert(polygon); result={layer=name} end
        function Attribute(key, value) result[key]=value end
        function MinZoom(value) result.zoom=value end
        function Area() return area end
        for _, sample in ipairs({{30000000,6}, {10000000,7}, {2000000,8}, {1000,14}}) do
            area=sample[1]
            WriteLanduse("quarry")
            assert(result.layer=="landuse" and result.class=="quarry")
            assert(result.zoom==sample[2])
        end
        """
        subprocess.run(["lua", "-"], input=script, text=True, cwd=ROOT, check=True)
