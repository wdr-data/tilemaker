#!/usr/bin/env python3
"""Write a standalone before/after land-use comparison from real MBTiles.

This enlarges the same geographic extent at each source zoom to expose changes
in geometry. It is not a screenshot at native screen scale and adds no labels.
Requires the tippecanoe-decode executable, but no Python packages.
"""

import json
import math
import subprocess
from collections.abc import Sequence
from pathlib import Path
from typing import Literal, TypedDict

Bounds = tuple[float, float, float, float]
LanduseSelection = Literal["residential", "built-up"]
BUILT_UP_CLASSES = frozenset(
    {
        "residential",
        "commercial",
        "industrial",
        "retail",
        "railway",
        "bus_station",
        "school",
        "university",
        "college",
        "kindergarten",
        "library",
        "hospital",
    }
)
Ring = list[list[float]]
Polygon = list[Ring]


class PolygonGeometry(TypedDict):
    type: Literal["Polygon"]
    coordinates: Polygon


class MultiPolygonGeometry(TypedDict):
    type: Literal["MultiPolygon"]
    coordinates: list[Polygon]


class Feature(TypedDict):
    geometry: PolygonGeometry | MultiPolygonGeometry
    properties: dict[str, object]


class DecodedLayer(TypedDict):
    features: list[Feature]


class DecodedTile(TypedDict):
    features: list[DecodedLayer]


def tile_xy(lon: float, lat: float, z: int) -> tuple[float, float]:
    return (
        (lon + 180) / 360 * 2**z,
        (1 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2 * 2**z,
    )


def features(
    tileset: Path,
    bbox: Bounds,
    zoom: int,
    decoder: str,
    classes: frozenset[str] = frozenset({"residential"}),
) -> list[Feature]:
    west, south, east, north = bbox
    left, top = tile_xy(west, north, zoom)
    right, bottom = tile_xy(east, south, zoom)
    result: list[Feature] = []
    for x in range(math.floor(left), math.floor(right) + 1):
        for y in range(math.floor(top), math.floor(bottom) + 1):
            # -f tolerates pre-existing winding defects in quantized tiny rings.
            proc = subprocess.run(
                [
                    decoder,
                    "-f",
                    "-l",
                    "landuse",
                    str(tileset),
                    str(zoom),
                    str(x),
                    str(y),
                ],
                capture_output=True,
                text=True,
                check=True,
            )
            tile: DecodedTile = json.loads(proc.stdout)

            for layer in tile["features"]:
                result.extend(
                    f
                    for f in layer["features"]
                    if str(f["properties"].get("class")) in classes
                )

    return result


def svg_panel(items: Sequence[Feature], bbox: Bounds) -> str:
    west, south, east, north = bbox
    left, top = tile_xy(west, north, 0)
    right, bottom = tile_xy(east, south, 0)
    width = 720
    height = width * (bottom - top) / (right - left)
    paths = []
    for feature in items:
        geom = feature["geometry"]

        if geom["type"] not in ("Polygon", "MultiPolygon"):
            continue

        polygons = (
            [geom["coordinates"]] if geom["type"] == "Polygon" else geom["coordinates"]
        )

        for polygon in polygons:
            segments = []
            for ring in polygon:
                for i, (lon, lat) in enumerate(ring):
                    x, y = tile_xy(lon, lat, 0)
                    segments.append(
                        f"{'M' if i == 0 else 'L'}{(x - left) / (right - left) * width:.2f},{(y - top) / (bottom - top) * height:.2f}"
                    )
                segments.append("Z")
            paths.append(f'<path d="{" ".join(segments)}"/>')

    return f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height:.2f}" style="background:#f1f3f4" overflow="hidden"><g fill="#b3b9be" fill-rule="evenodd">{"".join(paths)}</g></svg>'


def render(
    before: Path,
    after: Path,
    output: Path,
    bbox: Bounds,
    decoder: str,
    landuse: LanduseSelection = "built-up",
) -> None:
    classes = (
        (BUILT_UP_CLASSES | {"built_up"})
        if landuse == "built-up"
        else frozenset({"residential"})
    )
    zooms = range(6, 12) if landuse == "built-up" else range(6, 10)
    sections = []
    for z in zooms:
        panels = [
            svg_panel(features(path, bbox, z, decoder, classes), bbox)
            for path in [before, after]
        ]
        sections.append(
            f'<section data-zoom="{z}"><div><h2>Existing tileset</h2>{panels[0]}</div><div><h2>Candidate</h2>{panels[1]}</div></section>'
        )
    html = (
        """<!doctype html><meta charset="utf-8"><title>Land-use geometry comparison</title>
<style>body{font:16px system-ui;margin:32px;background:#fff;color:#303438}h1{font-size:24px}h2{font-size:17px}p{max-width:1000px;line-height:1.5}section{display:none;gap:24px}section.active{display:flex}section>div{width:50%;min-width:0}svg{width:100%;border:1px solid #ddd}select{font:inherit;padding:6px}footer{margin-top:24px;color:#60666a;font-size:14px}</style>
<h1>Land-use geometry comparison</h1>
<p>Selected land-use polygons only, with identical colors and geographic extent. The selected source zoom is enlarged to make boundaries visible; this is not native map scale. Labels and other map layers are omitted from this diagnostic view.</p>
<label>Source zoom <select id="zoom">ZOOM_OPTIONS</select></label>
"""
        + "".join(sections)
        + """<footer>Before: supplied tileset. After: local extract using the candidate config and production Lua. The regional extract can contain a different OSM snapshot; review a full candidate tileset before publication.</footer>
<script>const select=document.querySelector('#zoom');function show(){document.querySelectorAll('section').forEach(s=>s.classList.toggle('active',s.dataset.zoom===select.value))}select.onchange=show;show()</script>"""
    )
    html = html.replace("ZOOM_OPTIONS", "".join(f"<option>{z}</option>" for z in zooms))
    html = html.replace(
        "Selected land-use polygons only", f"Classes: {', '.join(sorted(classes))}"
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(html)
    print(output)
