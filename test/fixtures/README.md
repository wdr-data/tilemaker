# Overview geometry regression

`built-up-invalid-7-68-43.pbf.gz` is the original encoded tile from the Europe
260910 closing-candidate run, before its one-off repair. Tile coordinates are
XYZ. Data © OpenStreetMap contributors, licensed under ODbL:
https://www.openstreetmap.org/copyright

One `landuse/class=built_up` polygon has approximately 41,500 vertices and
self-intersections. MapLibre's Earcut triangulator took about 23 seconds on
this polygon; after repair the whole layer took 4–8 milliseconds locally.
The fixture also contains water, landcover, roads and non-built-up landuse,
so tests can verify the repair leaves unrelated encoded data intact.

The Python regression checks the decoded geometry, integer-grid round trip,
idempotence and preservation of other layers/attributes. It does not use a
machine-dependent timing threshold or require Node on the build server.

`regions-invalid-z3.pbf.gz` is tile 3/4/2 (XYZ)
from the regions overlay generated from Geofabrik DACH 2026-09-10, before final
integer geometry repair. OSM data © OpenStreetMap contributors, ODbL 1.0.
It exercises quantization defects while preserving region identifiers and outlines.
