"""Check the cross-config ownership rules that prevent duplicate map fills."""

import json
import re
import unittest
from pathlib import Path
from typing import NotRequired, TypedDict

ROOT = Path(__file__).resolve().parents[1]


class Layer(TypedDict):
    minzoom: int
    maxzoom: int
    source: NotRequired[str]
    write_to: NotRequired[str]
    simplify_below: NotRequired[int]
    simplify_level: NotRequired[float]
    simplify_ratio: NotRequired[float]


class ZoomSettings(TypedDict):
    minzoom: int
    maxzoom: int
    basezoom: int


class Config(TypedDict):
    layers: dict[str, Layer]
    settings: ZoomSettings


class ConfigContractTest(unittest.TestCase):
    def setUp(self) -> None:
        self.configs: dict[str, Config] = {}
        for region in ("coastline", "europe", "dach", "nrw"):
            filename = (
                "config-coastline.json"
                if region == "coastline"
                else f"config-openmaptiles-{region}.json"
            )
            self.configs[region] = json.loads(
                (ROOT / "resources" / filename).read_text()
            )

    def test_all_lua_output_layers_remain_declared_in_regional_configs(self) -> None:
        lua = (ROOT / "resources/process-openmaptiles.lua").read_text()
        required = set(re.findall(r'\bLayer(?:AsCentroid)?\("([^"\n]+)"', lua))
        # WritePOI passes these names indirectly to LayerAsCentroid.
        required.update(("poi", "poi_detail"))
        for region in ("europe", "dach", "nrw"):
            self.assertFalse(required - self.configs[region]["layers"].keys(), region)

    def test_static_shapes_have_one_owner_at_each_output_zoom(self) -> None:
        coastline = self.configs["coastline"]["layers"]
        self.assertEqual(
            {name for name, layer in coastline.items() if "source" in layer},
            {"ocean", "urban_areas", "ice_shelf", "glacier"},
        )
        self.assertFalse(
            any(
                "source" in layer for layer in self.configs["europe"]["layers"].values()
            )
        )
        for region in ("dach", "nrw"):
            self.assertEqual(
                {
                    name
                    for name, layer in self.configs[region]["layers"].items()
                    if "source" in layer
                },
                {"ocean"},
            )
        for source in ("ocean", "urban_areas", "ice_shelf", "glacier"):
            for zoom in range(15):
                owners = []
                for region, config in self.configs.items():
                    layer = config["layers"].get(source)
                    if layer and max(
                        config["settings"]["minzoom"], layer["minzoom"]
                    ) <= zoom <= min(config["settings"]["maxzoom"], layer["maxzoom"]):
                        owners.append(region)
                # Shared z13 ocean is resolved by whole-tile NRW priority.
                self.assertTrue(
                    len(owners) <= 1
                    or (source == "ocean" and zoom == 13 and owners == ["dach", "nrw"]),
                    (source, zoom, owners),
                )

    def test_zoom_ranges_urban_cutoff_and_matching_simplification(self) -> None:
        expected = {
            "coastline": (0, 12),
            "europe": (0, 12),
            "dach": (13, 13),
            "nrw": (13, 14),
        }
        for region, (minimum, maximum) in expected.items():
            settings = self.configs[region]["settings"]
            self.assertEqual(
                (settings["minzoom"], settings["maxzoom"]), (minimum, maximum)
            )
            self.assertGreaterEqual(settings["basezoom"], maximum)
        self.assertEqual(
            self.configs["coastline"]["layers"]["urban_areas"]["maxzoom"], 5
        )
        for region in ("europe", "dach", "nrw"):
            layers = self.configs[region]["layers"]
            for setting in ("simplify_below", "simplify_level", "simplify_ratio"):
                self.assertEqual(
                    layers["landuse"].get(setting),
                    layers["landcover"].get(setting),
                    (region, setting),
                )

    def test_write_to_targets_exist_before_their_aliases(self) -> None:
        for region, config in self.configs.items():
            seen = set()
            for name, layer in config["layers"].items():
                if "write_to" in layer:
                    self.assertIn(layer["write_to"], seen, (region, name))
                seen.add(name)


if __name__ == "__main__":
    unittest.main()
