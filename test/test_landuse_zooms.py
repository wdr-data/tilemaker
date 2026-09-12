"""Exercise the production Lua's overview policy using Tilemaker area/zoom hooks."""

import json
import re
import shutil
import subprocess
import unittest
from pathlib import Path

from wdr_tiles.preview import BUILT_UP_CLASSES

ROOT = Path(__file__).resolve().parents[1]


class LanduseZoomTest(unittest.TestCase):
    def test_preview_classes_match_production_lua(self) -> None:
        lua = (ROOT / "resources/process-openmaptiles.lua").read_text()
        match = re.search(r"builtUpLanduseKeys = Set\s*\{([^}]+)\}", lua)
        assert match is not None
        self.assertEqual(set(re.findall(r'"([^\"]+)"', match[1])), BUILT_UP_CLASSES)

    def test_overview_alias_ends_before_original_classes_enter(self) -> None:
        for region in ("europe", "dach", "nrw"):
            config = json.loads(
                (ROOT / f"resources/config-openmaptiles-{region}.json").read_text()
            )
            overview = config["layers"]["landuse_built_up"]
            self.assertEqual((overview["minzoom"], overview["maxzoom"]), (6, 9))
            self.assertEqual(overview["combine_polygons_below"], 10)
            self.assertEqual(overview["write_to"], "landuse")
            for key in ("simplify_below", "simplify_level"):
                self.assertEqual(overview[key], config["layers"]["landcover"][key])

    @unittest.skipUnless(shutil.which("lua"), "Lua interpreter required")
    def test_built_up_output_preserves_detail_and_admits_small_parcels(self) -> None:
        script = r"""
        dofile("resources/process-openmaptiles.lua")
        local outputs, current
        function Layer(name, polygon)
            assert(polygon)
            current = {layer=name}
            table.insert(outputs, current)
        end
        function Attribute(key, value) current[key] = value end
        function MinZoom(value) current.zoom = value end
        -- Area must not be consulted: small parcels need to reach the union.
        function Area() error("Pre-union area filtering would drop small parcels") end
        for _, class in ipairs({"residential", "commercial", "industrial", "retail",
            "railway", "bus_station", "school", "university", "college",
            "kindergarten", "library", "hospital"}) do
            outputs = {}
            WriteLanduse(class)
            assert(#outputs == 2)
            assert(outputs[1].layer == "landuse" and outputs[1].class == class and outputs[1].zoom == 10)
            assert(outputs[2].layer == "landuse_built_up" and outputs[2].class == "built_up" and outputs[2].zoom == 6)
        end
        for _, class in ipairs({"cemetery", "military", "stadium", "pitch",
            "playground", "theme_park", "zoo"}) do
            outputs = {}
            WriteLanduse(class)
            assert(#outputs == 1)
            assert(outputs[1].layer == "landuse" and outputs[1].class == class and outputs[1].zoom == 11)
        end
        """
        subprocess.run(["lua", "-"], input=script, text=True, cwd=ROOT, check=True)
