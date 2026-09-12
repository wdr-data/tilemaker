"""Selection boundaries shared by the overview and native detail export."""

import re
import shutil
import subprocess
import unittest
from pathlib import Path

from wdr_tiles.closing_geometry import PAVED_SURFACES, selected_class

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
