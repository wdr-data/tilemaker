"""Select German regions and derive fills, outlines and masks from one geometry."""

import json
import re
from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pyproj
import shapely
from shapely.geometry import box, mapping, shape
from shapely.geometry.base import BaseGeometry
from shapely.ops import transform

from .logging import log

WORLD_METRES = 40075016.68557849
WORLD = box(-180, -85.05112878, 180, 85.05112878)
PROJECT = pyproj.Transformer.from_crs(4326, 3857, always_xy=True).transform
UNPROJECT = pyproj.Transformer.from_crs(3857, 4326, always_xy=True).transform
STATE_CODES = frozenset("BW BY BE BB HB HH HE MV NI NW RP SL SN ST SH TH".split())


@dataclass(frozen=True)
class Region:
    kind: str
    code: str
    name: str
    geometry: BaseGeometry

    @property
    def properties(self) -> dict[str, str]:
        return {
            "id": f"{self.kind}:{self.code}",
            "kind": self.kind,
            "code": self.code,
            "name": self.name,
        }


def features(path: Path) -> Iterator[dict[str, Any]]:
    with path.open() as stream:
        for line in stream:
            yield json.loads(line)


def polygonal(value: BaseGeometry) -> BaseGeometry:
    """Repair invalid rings without keeping collapsed line or point remnants."""
    if not value.is_valid:
        value = shapely.make_valid(value, method="structure", keep_collapsed=False)
    if value.geom_type not in {"Polygon", "MultiPolygon"} or value.is_empty:
        raise ValueError(f"Region is not a nonempty polygon: {value.geom_type}")
    return value


def read_regions(path: Path) -> list[Region]:
    administrative: list[Region] = []
    for feature in features(path):
        tags = feature["properties"]
        if tags.get("boundary") != "administrative":
            continue
        code = tags.get("ISO3166-2", "")
        if tags.get("admin_level") == "2" and tags.get("ISO3166-1:alpha2") == "DE":
            kind, code = "country", "DE"
        elif tags.get("admin_level") == "4" and code.startswith("DE-"):
            kind = "state"
        else:
            continue
        administrative.append(
            Region(
                kind,
                code,
                tags.get("name", code),
                polygonal(shape(feature["geometry"])),
            )
        )
    countries = [r for r in administrative if r.kind == "country"]
    codes = [r.code for r in administrative if r.kind == "state"]
    if (
        len(countries) != 1
        or len(codes) != 16
        or set(codes) != {f"DE-{c}" for c in STATE_CODES}
    ):
        raise ValueError("Regions input must contain Germany and all 16 German states")
    germany = countries[0].geometry
    shapely.prepare(germany)
    postcodes: dict[str, list[BaseGeometry]] = {}
    for feature in features(path):
        tags = feature["properties"]
        code = tags.get("postal_code", "")
        if tags.get("boundary") != "postal_code" or not re.fullmatch(r"[0-9]{5}", code):
            continue
        geometry = polygonal(shape(feature["geometry"]))
        # The DACH input also contains neighbouring countries' postal regions.
        if not germany.covers(geometry.representative_point()):
            continue
        postcodes.setdefault(code, []).append(geometry)
    for code, parts in sorted(postcodes.items()):
        geometry = parts[0] if len(parts) == 1 else polygonal(shapely.union_all(parts))
        administrative.append(Region("postal", code, code, geometry))
    if not postcodes:
        raise ValueError("No German postal polygons found in regions input")
    log.info("regions.selected", counts=dict(Counter(r.kind for r in administrative)))
    return administrative


def zoom_geometry(projected: BaseGeometry, zoom: int) -> BaseGeometry:
    # Half a 512px map pixel. Quantize on the global MVT grid before deriving
    # the outline, so both shapes share the very same vertices at each zoom.
    tolerance = WORLD_METRES / (2**zoom * 512) * 0.5
    grid = WORLD_METRES / (2**zoom * 4096)
    simplified = projected.simplify(tolerance, preserve_topology=True)
    return shapely.set_precision(simplified, grid)


def feature_json(geometry: BaseGeometry, properties: dict[str, str]) -> str:
    return json.dumps(
        {"type": "Feature", "properties": properties, "geometry": mapping(geometry)},
        separators=(",", ":"),
    )


def write_zoom_sources(
    regions: list[Region], directory: Path, maxzoom: int
) -> dict[str, object]:
    projected = [(r, transform(PROJECT, r.geometry)) for r in regions]
    layers: dict[str, object] = {
        "regions": {"minzoom": 0, "maxzoom": maxzoom},
        "region_boundaries": {"minzoom": 0, "maxzoom": maxzoom},
    }
    for zoom in range(maxzoom + 1):
        fill_path = directory / f"fill-z{zoom}.geojsonl"
        line_path = directory / f"outline-z{zoom}.geojsonl"
        count = 0
        with fill_path.open("w") as fills, line_path.open("w") as lines:
            for region, geometry in projected:
                minzoom = {"country": 0, "state": 3, "postal": 7}[region.kind]
                if zoom < minzoom:
                    continue
                simplified = zoom_geometry(geometry, zoom)
                if simplified.is_empty:
                    continue
                geographic = transform(UNPROJECT, simplified)
                fills.write(feature_json(geographic, region.properties) + "\n")
                lines.write(feature_json(geographic.boundary, region.properties) + "\n")
                count += 1
        for name, path, target in (
            ("fill", fill_path, "regions"),
            ("outline", line_path, "region_boundaries"),
        ):
            layers[f"{name}_z{zoom}"] = {
                "minzoom": zoom,
                "maxzoom": zoom,
                "source": str(path),
                "source_columns": ["id", "kind", "code", "name"],
                "write_to": target,
                # Simplification is already shared by fill and outline above.
                "simplify_below": 0,
            }
        log.info("regions.zoom_prepared", zoom=zoom, features=count)
    return layers


def mask_collection(region: Region, maxzoom: int) -> dict[str, object]:
    geometry = transform(
        UNPROJECT, zoom_geometry(transform(PROJECT, region.geometry), maxzoom)
    )
    inverse = WORLD.difference(geometry)
    return {
        "type": "FeatureCollection",
        "features": [
            json.loads(feature_json(inverse, {**region.properties, "role": "outside"})),
            json.loads(
                feature_json(
                    geometry.boundary, {**region.properties, "role": "outline"}
                )
            ),
        ],
    }
