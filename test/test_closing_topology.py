"""Validate again after reprojection, where valid metric polygons can cross."""

import unittest
from collections.abc import Callable
from unittest.mock import patch

import shapely
from shapely.geometry import Polygon, box
from shapely.geometry.base import BaseGeometry
from shapely.ops import transform

from wdr_tiles import closing_geometry as geometry


class ClosingTopologyTest(unittest.TestCase):
    def test_valid_geometry_is_not_changed(self) -> None:
        original = box(6, 50, 7, 51)
        self.assertIs(
            geometry.valid_geometry(original, (6, 50, 7, 51), "test"), original
        )

    def test_invalid_reprojected_polygon_is_repaired_before_intersection(self) -> None:
        bounds = (6.0, 50.0, 7.0, 51.0)
        invalid = Polygon(
            [(6.2, 50.2), (6.8, 50.8), (6.2, 50.8), (6.8, 50.2), (6.2, 50.2)]
        )
        self.assertFalse(invalid.is_valid)
        _, inverse = geometry.transformers()

        def reproject(
            function: Callable[..., object], value: BaseGeometry
        ) -> BaseGeometry:
            if function == inverse.transform:
                return invalid
            return transform(function, value)

        with patch.object(geometry, "transform", side_effect=reproject):
            masks = geometry.cell_masks([box(6.1, 50.1, 6.9, 50.9)], bounds)
        expected = shapely.make_valid(invalid)
        self.assertEqual(set(masks), set(geometry.RADII))
        for wkb in masks.values():
            result = shapely.from_wkb(wkb)
            self.assertTrue(result.is_valid)
            self.assertLess(result.symmetric_difference(expected).area, 1e-12)

    def test_actual_cell_10_90_reprojection_defect(self) -> None:
        # Small invalid component captured from the Europe 260910 source at z6.
        # Keep full precision: rounding removes the self-intersection.
        value = shapely.from_wkt(
            "POLYGON ((5.111736958407117 45.005495053899345, "
            "5.111736149776237 45.00549792796958, "
            "5.111740799999993 45.00549860000001, "
            "5.111740799999938 45.0054986, "
            "5.111742026396172 45.00549672279938, "
            "5.111736958407117 45.005495053899345))"
        )
        self.assertFalse(value.is_valid)
        repaired = geometry.valid_geometry(value, (5, 45, 5.5, 45.5), "geographic", 6)
        result = repaired.intersection(box(5, 45, 5.5, 45.5))
        self.assertTrue(result.is_valid)
        self.assertFalse(result.is_empty)
        self.assertGreater(result.area, 0)
        self.assertAlmostEqual(result.area, value.area, delta=1e-18)
