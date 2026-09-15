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

## Forest disappearing between z13 and z14

`forest-1315781.osm.pbf` contains OSM relation
[1315781](https://www.openstreetmap.org/relation/1315781), its 54 member ways and
referenced nodes, extracted from Geofabrik DACH 2026-09-10. OSM data
© OpenStreetMap contributors, licensed under ODbL 1.0.

The valid source forest near Jägersief (6.27645, 50.50824) disappeared at z14
when an invalid z13 clipped polygon was reused from the ancestor cache.
`test/test_forest_clipping.py` runs the native executable with the production
NRW config/Lua (without external coastline data). It checks forest coverage,
two real clearings, polygon validity and MVT ring winding at both zooms,
with one/four workers and with z14 generated independently. It skips if the
native executable has not been built. The pre-fix executable fails the test.

To reproduce the fixture from the original, non-renumbered input:

```bash
osmium getid -r osm/dach-260910.osm.pbf r1315781 -o test/fixtures/forest-1315781.osm.pbf
```
