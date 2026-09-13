"""Bounded geometry work for one half-degree cell of the built-up overview.

Closing is computed with a halo, then clipped to an exact lon/lat cell. The
GeoJSON pieces are unioned again by Tilemaker before simplification, removing
internal cell edges. A shared projection avoids discontinuities at UTM zones.
"""

import math
import sqlite3
from collections.abc import Iterator, Mapping
from contextlib import closing
from functools import lru_cache
from pathlib import Path

import pyproj
import shapely
from shapely.geometry import Polygon, box
from shapely.geometry.base import BaseGeometry
from shapely.ops import transform

from .logging import log
from .preview import BUILT_UP_CLASSES

RADII = {6: 100, 7: 75, 8: 50, 9: 25}
CELL_DEGREES = 0.5
HALO_METRES = 2000
WATERWAYS = {"river", "riverbank", "stream", "canal", "drain", "ditch", "dock"}
PAVED_SURFACES = frozenset(
    {
        "bricks",
        "paved",
        "asphalt",
        "paving_stones",
        "cobblestone",
        "concrete",
        "concrete:lanes",
        "concrete:plates",
        "metal",
        "wood",
        "sett",
        "unhewn_cobblestone",
    }
)
# Mapped road surfaces we can safely consider when explicitly paved.
# Deliberately excludes traffic islands and unspecified path areas.
PAVED_HIGHWAYS = frozenset(
    {
        "pedestrian",
        "footway",
        "cycleway",
        "steps",
        "service",
        "living_street",
        "residential",
        "unclassified",
        "road",
        "tertiary",
        "secondary",
        "primary",
        "trunk",
        "motorway",
        "tertiary_link",
        "secondary_link",
        "primary_link",
        "trunk_link",
        "motorway_link",
    }
)
Cell = tuple[int, int]
Bounds = tuple[float, float, float, float]


def selected_class(tags: Mapping[str, str]) -> str | None:
    """Match the normal landuse branch and early exclusions in production Lua."""
    if tags.get("disused") == "yes" or tags.get("highway") == "proposed":
        return None
    if (
        tags.get("natural") == "water"
        or tags.get("leisure") == "swimming_pool"
        or tags.get("landuse") in {"reservoir", "basin"}
        or tags.get("waterway") in WATERWAYS
    ):
        return None
    value = next(
        (
            tags[key]
            for key in ("landuse", "natural", "leisure", "amenity", "tourism")
            if tags.get(key)
        ),
        "",
    )
    if value in BUILT_UP_CLASSES:
        return value
    # Physical land cover takes precedence. A parking/marketplace amenity is
    # allowed through only so its explicit surface can be checked below.
    if value and value not in {"parking", "marketplace"}:
        return None
    try:
        layer = float(tags.get("layer", "0"))
    except ValueError:
        layer = 0
    if (
        tags.get("surface") not in PAVED_SURFACES
        or tags.get("tunnel", "") not in {"", "no"}
        or tags.get("covered", "") not in {"", "no"}
        or tags.get("indoor", "") not in {"", "no"}
        or layer < 0
        or tags.get("area") == "no"
    ):
        return None
    # Only called for exported polygon geometry. Osmium drops type=multipolygon
    # from tags; --attributes=type retains the assembled relation's provenance.
    explicit_area = (
        tags.get("area") == "yes"
        or tags.get("type") == "multipolygon"
        or tags.get("@type") == "relation"
    )
    area_highway = tags.get("area:highway", "")
    highway = tags.get("highway", "")
    if area_highway in PAVED_HIGHWAYS:
        return area_highway
    if highway in PAVED_HIGHWAYS and explicit_area:
        return highway
    if tags.get("amenity") == "parking":
        return "parking" if tags.get("parking", "surface") == "surface" else None
    if tags.get("place") == "square" or tags.get("amenity") == "marketplace":
        return "square"
    return None


def polygons(geometry: BaseGeometry) -> Iterator[Polygon]:
    if isinstance(geometry, Polygon):
        if not geometry.is_empty:
            yield geometry
    else:
        for part in getattr(geometry, "geoms", ()):
            yield from polygons(part)


def cell_bounds(cell: Cell) -> Bounds:
    x, y = cell
    return (
        x * CELL_DEGREES,
        y * CELL_DEGREES,
        (x + 1) * CELL_DEGREES,
        (y + 1) * CELL_DEGREES,
    )


def covering_cells(bounds: Bounds) -> Iterator[Cell]:
    west, south, east, north = bounds
    for x in range(
        math.floor(west / CELL_DEGREES), math.floor(east / CELL_DEGREES) + 1
    ):
        for y in range(
            math.floor(south / CELL_DEGREES), math.floor(north / CELL_DEGREES) + 1
        ):
            yield x, y


def padded_bounds(bounds: Bounds) -> Bounds:
    west, south, east, north = bounds
    # Conservative geographic envelope for a 2 km halo, much wider than the
    # maximum 200 m dependency of a 100 m closing operation.
    latitude = min(85, max(abs(south), abs(north)) + 0.1)
    dy = HALO_METRES / 100_000
    dx = dy / math.cos(math.radians(latitude))
    return west - dx, max(-85, south - dy), east + dx, min(85, north + dy)


@lru_cache(maxsize=1)
def transformers() -> tuple[pyproj.Transformer, pyproj.Transformer]:
    return (
        pyproj.Transformer.from_crs(4326, 3035, always_xy=True),
        pyproj.Transformer.from_crs(3035, 4326, always_xy=True),
    )


def close(geometry: BaseGeometry, radius: float) -> BaseGeometry:
    """Dissolve expanded offsets and shrink them back (metre coordinates)."""
    return shapely.buffer(
        shapely.buffer(geometry, radius, quad_segs=8), -radius, quad_segs=8
    )


def valid_geometry(
    value: BaseGeometry, bounds: Bounds, stage: str, zoom: int | None = None
) -> BaseGeometry:
    """Coordinate transforms can invalidate even previously valid polygons."""
    if value.is_valid:
        return value
    reason = shapely.is_valid_reason(value)
    repaired = shapely.make_valid(value)
    if not repaired.is_valid:
        raise ValueError(
            f"Could not repair {stage} geometry at {bounds}, z{zoom}: {reason}"
        )
    log.warning(
        "built_up.geometry_repaired",
        bounds=bounds,
        stage=stage,
        zoom=zoom,
        reason=reason,
    )
    return repaired


def cell_masks(geometries: list[BaseGeometry], bounds: Bounds) -> dict[int, bytes]:
    project, unproject = transformers()
    halo = box(*padded_bounds(bounds))
    projected = []
    for geometry in geometries:
        if geometry.intersects(halo):
            # Project the full source geometry before clipping to avoid different
            # projections of a long source segment on opposite sides of a cell.
            projected.append(
                valid_geometry(
                    transform(project.transform, geometry), bounds, "projected"
                )
            )
    if not projected:
        return {}
    base = valid_geometry(shapely.union_all(projected), bounds, "union")
    core = box(*bounds)
    result = {}
    for zoom, radius in RADII.items():
        closed = valid_geometry(close(base, radius), bounds, "closed", zoom)
        geographic = valid_geometry(
            transform(unproject.transform, closed), bounds, "geographic", zoom
        )
        clipped = valid_geometry(geographic.intersection(core), bounds, "clipped", zoom)
        parts = list(polygons(clipped))
        if parts:
            result[zoom] = shapely.to_wkb(shapely.MultiPolygon(parts))
    return result


def process_cell(database: Path, cell: Cell) -> dict[int, bytes]:
    bounds = cell_bounds(cell)
    west, south, east, north = padded_bounds(bounds)
    with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as db:
        rows = db.execute(
            """SELECT p.geometry FROM bounds b JOIN polygons p ON p.id=b.id
            WHERE b.minx<=? AND b.maxx>=? AND b.miny<=? AND b.maxy>=?""",
            (east, west, north, south),
        )
        geometries = [shapely.from_wkb(row[0]) for row in rows]
    return cell_masks(geometries, bounds)
