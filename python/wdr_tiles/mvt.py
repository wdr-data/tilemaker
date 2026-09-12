"""Lossless protobuf fields and polygon commands for a narrow MVT repair.

Keep original field bytes so changing polygon geometry cannot reorder properties,
coerce attribute types, or re-encode unrelated layers. Coordinates stay in the
integer tile grid, including the buffer outside the tile's extent.
"""

from collections.abc import Iterator, Sequence
from dataclasses import dataclass

import shapely
from shapely.geometry import Polygon

Point = tuple[int, int]
Ring = list[Point]


@dataclass(frozen=True)
class Field:
    tag: int
    wire: int
    payload: bytes
    raw: bytes


def read_varint(data: bytes, position: int) -> tuple[int, int]:
    value = 0
    for shift in range(0, 70, 7):
        if position >= len(data):
            raise ValueError("Truncated protobuf varint")
        byte = data[position]
        position += 1
        value |= (byte & 127) << shift
        if byte < 128:
            return value, position
    raise ValueError("Oversized protobuf varint")


def varint(value: int) -> bytes:
    if value < 0:
        raise ValueError("Unsigned varint cannot be negative")
    result = bytearray()
    while value > 127:
        result.append((value & 127) | 128)
        value >>= 7
    result.append(value)
    return bytes(result)


def fields(data: bytes) -> Iterator[Field]:
    position = 0
    while position < len(data):
        start = position
        key, position = read_varint(data, position)
        tag, wire = key >> 3, key & 7
        if tag == 0:
            raise ValueError("Invalid protobuf field tag")
        if wire == 2:
            size, position = read_varint(data, position)
            end = position + size
        elif wire == 0:
            _, end = read_varint(data, position)
        elif wire in (1, 5):
            end = position + (8 if wire == 1 else 4)
        else:
            raise ValueError(f"Unsupported protobuf wire type {wire}")
        if end > len(data):
            raise ValueError("Truncated protobuf field")
        yield Field(tag, wire, data[position:end], data[start:end])
        position = end


def message(tag: int, payload: bytes) -> bytes:
    return varint((tag << 3) | 2) + varint(len(payload)) + payload


def numbers(data: bytes) -> list[int]:
    result = []
    position = 0
    while position < len(data):
        value, position = read_varint(data, position)
        result.append(value)
    return result


def rings(data: bytes) -> Iterator[Ring]:
    commands = numbers(data)
    position = x = y = 0
    ring: Ring = []
    while position < len(commands):
        command = commands[position]
        position += 1
        operation, count = command & 7, command >> 3
        if operation in (1, 2) and count:
            if operation == 1 and (count != 1 or ring):
                raise ValueError("Unexpected polygon MoveTo")
            if operation == 2 and not ring:
                raise ValueError("Polygon LineTo before MoveTo")
            if position + count * 2 > len(commands):
                raise ValueError("Truncated polygon coordinates")
            for _ in range(count):
                dx, dy = commands[position : position + 2]
                position += 2
                x += (dx >> 1) ^ -(dx & 1)
                y += (dy >> 1) ^ -(dy & 1)
                ring.append((x, y))
        elif operation == 7 and count == 1 and len(ring) >= 3:
            ring.append(ring[0])
            yield ring
            ring = []
        else:
            raise ValueError(f"Unexpected polygon command {command}")
    if ring:
        raise ValueError("Unclosed polygon")


def polygons(data: bytes) -> list[Polygon]:
    """Match MapLibre's winding inference, retaining every hole.

    Tilemaker can emit the opposite winding to the MVT convention. Like MapLibre,
    use the first nonzero-area ring to identify exteriors within each feature.
    Zero-area rings do not draw and must not change the inferred winding.
    """
    result = []
    group: list[Ring] = []
    outer_positive = None
    for ring in rings(data):
        area = sum(
            a[0] * b[1] - b[0] * a[1] for a, b in zip(ring, ring[1:], strict=False)
        )
        if not area:
            continue
        positive = area > 0
        if outer_positive is None:
            outer_positive = positive
        if positive == outer_positive and group:
            result.append(Polygon(group[0], group[1:]))
            group = []
        group.append(ring)
    if group:
        result.append(Polygon(group[0], group[1:]))
    return result


def encode_polygons(polygons: Sequence[Polygon]) -> bytes:
    output = bytearray()
    x = y = 0
    for polygon in polygons:
        # Positive exterior area means clockwise in MVT's downward y axis.
        polygon = shapely.orient_polygons(polygon)
        for ring in (polygon.exterior, *polygon.interiors):
            coordinates = list(ring.coords)[:-1]
            if len(coordinates) < 3:
                raise ValueError("Degenerate repaired ring")
            for index, (px, py) in enumerate(coordinates):
                if px != int(px) or py != int(py):
                    raise ValueError("Non-integer repaired coordinate")
                if index == 0:
                    output += varint(9)  # MoveTo, one point
                elif index == 1:
                    output += varint(((len(coordinates) - 1) << 3) | 2)
                dx, dy = int(px) - x, int(py) - y
                output += varint(2 * dx if dx >= 0 else -2 * dx - 1)
                output += varint(2 * dy if dy >= 0 else -2 * dy - 1)
                x, y = int(px), int(py)
            output += varint(15)  # ClosePath
    return bytes(output)
