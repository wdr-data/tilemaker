"""Closed ferry circuits are transport lines, never filled built-up areas."""

import shutil
import subprocess
import unittest
from pathlib import Path

from wdr_tiles.closing_geometry import selected_class

ROOT = Path(__file__).resolve().parents[1]


class FerryRouteTest(unittest.TestCase):
    def test_ferry_is_not_selected_for_built_up_overviews(self) -> None:
        self.assertIsNone(
            selected_class({"route": "ferry", "name": "Fahrgastschiff Möwe"})
        )

    @unittest.skipUnless(shutil.which("lua"), "Lua interpreter required")
    def test_open_and_closed_ferries_keep_line_geometry_and_names(self) -> None:
        script = r"""
        dofile("resources/process-openmaptiles.lua")
        local tags = {route="ferry", name="Fahrgastschiff Möwe"}
        local closed, output, current
        function Find(key) return tags[key] or "" end
        function Holds(key) return tags[key] ~= nil end
        function IsClosed() return closed end
        function IsMultiPolygon() return false end
        function NextRelation() return nil end
        function Area() return 1000000 end
        function ZOrder(value) end
        function Layer(name, polygon)
            current = {source_layer=name, polygon=polygon}
            table.insert(output, current)
        end
        function Attribute(key, value) current[key]=value end
        AttributeInteger = Attribute
        AttributeBoolean = Attribute
        function MinZoom(value) current.zoom=value end
        for _, is_closed in ipairs({false, true}) do
            closed, output = is_closed, {}
            assert(BuiltUpClass() == nil)
            way_function()
            assert(#output == 2, "ferry should only emit geometry and its label")
            assert(output[1].source_layer == "transportation")
            assert(output[1].polygon == false, "closed ferry must remain a line")
            assert(output[1].class == "ferry" and output[1].zoom == 9)
            assert(output[1].built_up == nil)
            assert(output[2].source_layer == "transportation_name")
            assert(output[2].polygon == false and output[2].zoom == 12)
            assert(output[2].class == "ferry")
            assert(output[2][preferred_language_attribute] == tags.name)
        end
        """
        subprocess.run(["lua", "-"], input=script, text=True, cwd=ROOT, check=True)
