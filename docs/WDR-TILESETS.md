# WDR regional tilesets

## Start a cloud build

Use **Ubuntu 24.04 LTS**, with a roomy local SSD for inputs, intermediate data,
logs, and output tiles. The example `.env` targets the planned 64-core / 512 GB
host. Run as root or as a user with passwordless sudo for package installation.

After cloning this repository and installing [uv](https://docs.astral.sh/uv/getting-started/installation/):

```bash
cp .env.example .env
# Edit .env: Geofabrik snapshot date and output location.
uv run tiles plan
uv run tiles run
```

`run` performs these steps in order:

1. Install missing native build dependencies via apt if enabled in `.env`;
   compile Tilemaker here and a pinned Tippecanoe revision in `.tools/`.
2. Download and checksum the dated Europe/DACH originals from Geofabrik, then
   renumber them (or reuse supplied renumbered inputs); prepare static shapefiles.
3. Extract the padded NRW buffer from Europe and renumber it.
4. Build coastline, Europe, DACH, then NRW, one at a time.
5. Merge into `OUTPUT_DIR/nrw-v4.mbtiles`, giving NRW priority at shared coordinates.

## Download and renumber on the server

The example enables `GEOFABRIK_DATE=260910`. Choose a date in YYMMDD format
available on **both** the [Europe](https://download.geofabrik.de/europe.html) and
[DACH](https://download.geofabrik.de/europe/dach.html) download pages. Both URLs
use that date, rather than rolling `-latest` files, so downloads can resume
against the same snapshot. An unavailable date fails explicitly.

To prepare inputs separately from the full build:

```bash
uv run tiles setup                 # Native tools, if not already installed
uv run tiles prepare-inputs        # Both Europe and DACH
# Or select one region:
uv run tiles prepare-inputs europe
uv run tiles prepare-inputs dach
```

`uv run tiles run` also performs this preparation automatically. Original PBFs
and Geofabrik `.md5` files are stored beside the configured renumbered outputs.
The command verifies each original against its published MD5 before running
`osmium renumber`. Regions are processed sequentially. Downloads resume from
`.part` files; interrupted renumbering restarts that region from its original.
MD5 here detects incomplete/corrupt downloads; it is not a security signature.

Keep the originals and the renumbered files' `.build.json` sidecars for resume
checks. A completed renumbering is reused only when the source identity,
snapshot, checksum, and Osmium binary record still match. Existing unstamped
renumbered files are not overwritten: use the supplied-input route below for
those. Allow disk space for both originals and renumbered files at once.

With empty path overrides, the snapshot sets the default filenames, including
the NRW extract. For a new snapshot, choose a new `OUTPUT_DIR` too. Explicit
`EUROPE_INPUT`, `DACH_INPUT`, and `NRW_INPUT` settings keep their configured paths.

### Use manually supplied files instead

Leave `GEOFABRIK_DATE` empty and set `EUROPE_INPUT`/`DACH_INPUT` to the renumbered
files you supply manually. Missing files produce an error; the pipeline does
not download replacements. If you downloaded original Geofabrik PBFs manually,
place them at the dated original paths described above and use `prepare-inputs`
to checksum and renumber them.

Use files from the same snapshot and wait for renumbering/download to finish.
The build uses `--compact`, which requires contiguous IDs. Optional
`EUROPE_SHA256`/`DACH_SHA256` refer to **renumbered** files, including outputs
created on the server. For supplied inputs, headers are checked with
`osmium fileinfo`; that is not proof of renumbering or a full integrity scan.

`uv` installs the Python package and locked Python dependencies. Native packages
are separate: automatic apt setup requires `INSTALL_SYSTEM_PACKAGES=true`, as in
the cloud example. With it disabled, install the packages listed in
`python/wdr_tiles/setup.py` yourself. Existing tools on PATH are reused; explicit
`TILEMAKER`, `TILE_JOIN`, `TILE_DECODE`, and `OSMIUM` overrides are also supported.
The auto-built Tippecanoe revision is pinned in that module.

For an unattended run, start the command in your usual persistent terminal
session (such as tmux), so disconnecting SSH does not terminate it.

## Configuration, logs, and resuming

Settings are validated by `pydantic-settings` and listed in `.env.example`.
Invalid booleans, non-positive thread counts, snapshot dates, and checksum formats
fail before building. Shell environment variables take
precedence over `.env`; relative configured paths are resolved against the repo.
Use `uv run tiles --env-file /path/to/build.env run` for a separate configuration.
Without `.env`, defaults use at most eight threads, no fast mode, and no automatic
system package installation. Copying the example opts into the cloud settings.

Logging uses `structlog`: readable events go to stderr, and the same events are
appended as JSON lines to `OUTPUT_DIR/logs/pipeline.jsonl`. Each command gets a
unique `run_id`. The event log includes the resolved settings, UTC timestamps,
command arguments, working directory, stage log path, elapsed seconds, return
codes/signals, reused/published files, removed overlap counts, and exception
tracebacks. Credentials and query strings in command URLs are redacted.

Native stdout and stderr remain together in `OUTPUT_DIR/logs/<stage>.log`.
Each attempt has a header with its run ID; failed attempts are not overwritten.
Long native commands emit `native.running` events every 60 seconds with their
PID and elapsed time. To inspect a running or failed build:

```bash
tail -f tilesets/closing-candidate/logs/pipeline.jsonl
tail -f tilesets/closing-candidate/logs/build-europe.log
```

Start with `pipeline.failed`/`native.failed` and follow the `native_log` path for
native diagnostics. A negative return code records a terminating signal such as
SIGKILL; proving an OOM kill still requires the server's kernel logs. No process
can record its own completion after a machine shutdown or an uncatchable kill.

Re-running `uv run tiles run` reuses matching completed steps. An interrupted
native build restarts that step; it does not resume inside Tilemaker. Downloads
resume separately. Completed files are published without overwriting existing
ones; failed build temporaries are removed after the child exits. An abrupt kill
can leave hidden work directories, which may be removed once no process uses them.

Each MBTiles contains a build record with configuration/Lua hashes, native binary
hash, source file identity (path, size and modification time), and applicable
static-source identities. Europe also records the generated built-up masks and
their manifest, which tracks the closing code and geometry library versions.
PBFs generated by this pipeline have `.build.json` sidecars; keep them with the files. Merge validates all four builds. Changed inputs or unstamped old outputs
cause an error instead of silently reusing stale tiles. Choose a new `OUTPUT_DIR`
for changed builds; also choose a new `NRW_INPUT` when changing the Europe source.
Performance settings such as thread count and fast mode do not invalidate
completed geometry. File identity checks are inexpensive resume checks, not
cryptographic verification of all large intermediate files.

Changing source paths counts as changing inputs. Keep inputs and shapefiles
available through the merge: their identities are checked again. Don't change
source files or binaries while a build is running.

Run a single step when needed:

```bash
uv run tiles setup
uv run tiles download
uv run tiles extract-nrw
uv run tiles build coastline
uv run tiles prepare-built-up       # Optional: build europe/run do this automatically
uv run tiles build europe
uv run tiles build dach
uv run tiles build nrw
uv run tiles merge
```

Individual steps assume their prerequisites are ready. Use the Python commands
above. Settings are listed in `.env.example`.

The code is split by responsibility: `cli.py` wires commands, `settings.py`
loads settings, `pipeline.py` lists the steps, `setup.py` prepares tools/static data, `inputs.py` downloads and renumbers OSM,
`closing.py` prepares overview masks, `indexing.py` prepares their spatial index, `closing_geometry.py` processes one spatial chunk,
`tile_repair.py` validates and repairs encoded overview tiles, `mvt.py` preserves their protobuf fields,
`logging.py` configures structlog, `state.py` handles native commands/resume/publication, and `mbtiles.py` removes overlaps.

## Memory and runtime

The cloud example uses RAM storage and `FAST=true`. This avoids the old disk
sharding path. Tilemaker's [running guide](../docs/RUNNING.md) describes fast mode's
higher memory consumption. 512 GB gives substantial headroom, but a small-area
preview cannot establish full-Europe peak memory or runtime. More cores will help
parallel stages; extraction, input scanning, and SQLite work will not all scale
linearly. Regions run sequentially to avoid multiplying peak memory use.

If RAM is insufficient, set `STORE_DIR` to local SSD storage. In this version,
`--store` automatically enables sharding **unless `--fast` is set**. Thus
`STORE_DIR=/fast-ssd/tilemaker` with `FAST=false` is the lower-memory, slower
option; with `FAST=true` disk storage is unsharded. This behavior is explicit in
`src/options_parser.cpp`. No separate sharding switch is needed in `.env`.

Allow disk space for both original inputs, the extracted and renumbered NRW
files, the temporary filtered built-up PBF/GeoJSON/spatial index and generated
masks, all four tilesets, filtered merge inputs, and the final merged file at once. No full-Europe runtime or peak-storage benchmark has been measured for
this pipeline; do not size storage from the final MBTiles alone.

## Coverage and ownership

| Input | Zooms | Purpose |
| --- | --- | --- |
| coastline | 0–12 | Global ocean/background shapes; urban outlines only at 4–5 |
| Europe | 0–12 | OSM data; closed built-up overview at 6–9; original classes from 10 |
| DACH | 13 | More detail outside the NRW buffer |
| NRW buffer | 13–14 | Highest priority wherever a tile exists |

The NRW buffer is `4.21,48.51,11.77,53.44`, not the state boundary. Extraction uses
a larger `4.1,48.4,11.9,53.55` rectangle to preserve geometry at the output edges.
At shared zooms, the higher-priority regional input owns the **whole tile**.
Lower-priority overlapping tiles are excluded before `tile-join`, leaving
originals untouched. This prevents duplicate transparent fills, labels, and
roads while keeping NRW's additional coverage outside DACH.

Global static shapes occur only in coastline at z0–12; generated built-up masks
are imported into Europe at z6–9. DACH and NRW supply ocean at
z13–14. Rebuild coastline as well as Europe: the old coastline file contained
Natural Earth urban polygons through z8, which caused the broad grey blankets.

Urban land-use and forest land-cover retain the same simplification:
`simplify_below: 13`, `simplify_level: 0.0003`, default `simplify_ratio: 2`.
Natural Earth urban shapes end at z5. At z6–9, selected OSM built-up polygons
are unioned and closed before simplification into `landuse/class=built_up`. At z10+ the
original classes return. See the built-up section below for the selection.

## Why the regional configs differ

The 20 OSM layer definitions are identical across Europe, DACH, and NRW.
The Python pipeline adds four generated mask inputs to Europe at build time.
The removed entries are shapefile inputs, some of which write into existing
output layers using `write_to`:

| Input layer | Output layer | Owner |
| --- | --- | --- |
| `ocean` | `water` | coastline at z0–12; DACH/NRW at z13–14, with NRW winning overlap |
| `urban_areas` | `landuse` | coastline only, z4–5 |
| `ice_shelf` | `landcover` | coastline only, z0–9 |
| `glacier` | `landcover` | coastline only, z2–9 |

Removing these imports from Europe avoids generating the same static polygons
twice. The removed Natural Earth imports in DACH/NRW already ended below those
builds' z13 start. OSM glacier/ice polygons still go through `landcover` at higher
zooms; they were not removed along with the Natural Earth shapefiles.

The effective zoom range is the intersection of the layer's range and the
config's `settings.minzoom/maxzoom` (with additional feature thresholds in Lua).
Europe therefore has no buildings, house numbers, detailed POIs, or water-name
labels above its z12 ceiling. At DACH z13, buildings are eligible but the z14-only
layers remain absent. All layer definitions stay available to the shared Lua:
Tilemaker raises an error if Lua writes to an undeclared layer, even when that
layer would not appear in the requested output zooms.

`test/test_config_contract.py` checks these ownership/zoom rules, Lua layer
references, `write_to` targets, and matching landuse/landcover simplification.
The generated overview inputs are checked by `test/test_landuse_zooms.py`;
geometry, processing seams and mask publication are covered by `test/test_closing.py`.

## Encoded overview geometry repair

Europe builds and previews automatically repair `landuse/class=built_up` at
zooms 6–9 after Tilemaker finishes. This runs on the final integer tile
coordinates: even valid source masks can acquire self-intersections during
Tilemaker's clipping, union and encoding operations. Such polygons can send
MapLibre's triangulator into a very slow fallback. In the first closing
candidate, a single polygon in tile `7/68/43` took about 23 seconds to triangulate.

The repair preserves exterior/hole roles with Shapely's
`make_valid(method="structure", keep_collapsed=False)`, then snaps to the
one-unit tile grid with `set_precision`. Each changed polygon is encoded,
decoded again, checked for validity and compared to the repaired geometry.
This adds no simplification or morphological closing. Other layers, classes,
feature attributes and zoom levels keep their original bytes. Tiles without
invalid built-up polygons remain byte-identical, including compression.

The step uses `BUILT_UP_WORKERS`, capped at 16, with a bounded queue and one
SQLite writer. Changes happen in one transaction on the unpublished Europe
file. Any error rolls back the repair and prevents publication. Repair runs
before merge, so the merged candidate inherits the corrected geometry.

`overview_repair.started`, `.progress`, `.running`, `.completed` and `.failed`
events record progress, counts and failures. Tile errors use XYZ coordinates.
The MBTiles metadata `wdr:geometry_repair` records the repair's code/library
versions and counts; Europe's build record also includes that provenance.
Repair code changes invalidate Europe outputs without invalidating reusable
closing masks or the other regional builds. Previously generated files,
including the manually repaired candidate, are not silently accepted under
the new build record. Use a new output location for the next candidate.

The checked-in regression tile and `test/test_tile_repair.py` cover the actual
slow geometry, preservation of holes and non-target data, both ring windings,
malformed input, transactional rollback, parallel processing and publication.
The manual repair of the first candidate checked all 9,133 overview tiles;
its slowest built-up triangulation was about 16 milliseconds locally. Runtime
figures describe that dataset and machine, not a performance guarantee.

## Python development checks

Ruff and ty are locked development dependencies, installed by `uv sync` or
`uv run`. From the repository root:

```bash
uv run ruff format
uv run ruff check
uv run ty check
```

For verification without changing files, use `uv run ruff format --check`.
Ruff checks imports, common errors, modern Python syntax, and missing function
annotations. ty checks the pipeline and Python tests against Python 3.11, the
minimum supported version. Both tools are configured in `pyproject.toml`.

## Preview and checks

```bash
uv run tiles preview --before tilesets/nrw-v4.mbtiles --directory /tmp/ruhr-preview
uv run python -m unittest discover -s test -p 'test_*.py' -v
```

The preview extracts a small Ruhr area from the configured DACH input and uses
the production Lua with the Europe config limited to z6–11 by default. Its `comparison.html`
enlarges identical extents at each source zoom to expose geometry changes. It
is a geometry diagnostic, not a native-scale screenshot. Use `--bbox` to choose
another area and a new `--directory` for each attempt.

Review the candidate in the actual map style at overview zooms and z13/14 buffer
edges before publishing. The earlier local audit sampled eastern and northern
buffer edges at z13: transportation, waterway, and administrative crossings
matched within two MVT coordinate units. This is sampled evidence, not a
whole-boundary guarantee, particularly for mismatched snapshots.

## Sparse coverage: intentionally no fallback in this version

Keep serving the final MBTiles through the existing mbtileserver/CloudFront
setup. No custom endpoint is required. This pipeline **does not generate missing
high-zoom tiles** and the experimental live fallback server has been removed.
Outside NRW, z14 requests can therefore still be empty; outside DACH, this can
already happen at z13. A single TileJSON `maxzoom` cannot express these regional
limits. Lowering it also prevents MapLibre from requesting the real NRW detail.

Offline filling remains a possible future build step. It would need to clip and
scale parent geometry into each missing child, preserve every real child tile,
and account for storage growth. Copying parent tile bytes to child coordinates
or indiscriminately overzooming all merged inputs is not correct.

## Rivers at the NRW overview

Named OSM `waterway=river` lines start at z7, so rivers such as the Rhine
remain visible when the full state is shown. This selects named rivers, not
only large rivers; streams, canals, drains, ditches and unnamed river lines
remain in the detail input starting at z12. River labels keep their existing
thresholds. Water polygons still use their area thresholds; the river lines
provide continuity when individual polygon sections are too small to survive.
The app's existing waterway style already draws these lines at z7.

## Built-up land on a green background

Use a green background and an explicit bright fill for built-up classes.
At z6–9, Python prepares a combined `landuse/class=built_up` mask with
morphological closing: expand the selected polygons, dissolve overlaps, then
shrink back by the same distance. This bridges narrow gaps while retaining the
broader shape of settlements. The distances are buffer radii; a gap somewhat
less than twice the radius can join, depending on its shape.

| Source zoom | Closing radius |
| --- | --- |
| 4–5 | Existing Natural Earth residential outlines |
| 6 | 100 m |
| 7 | 75 m |
| 8 | 50 m |
| 9 | 25 m |
| 10+ | Original classes, without closing |

The original classes start at z10, so the mask and originals never stack at the
same zoom. All selected parcels enter preparation regardless of area. The
existing forest-matched simplification still applies after closing. Buffering
can absorb small holes or green corridors; separate landcover and water fills
should remain above the built-up fill in the map style.

`prepare-built-up` reads an Osmium polygon export in bounded batches. Worker
processes parse, validate/repair and serialize polygons; one writer inserts
ordered batches into a SQLite spatial index.
It then processes half-degree cells with a 2 km halo. All cells use the same metric
European projection (EPSG:3035). Results are clipped to exact cell boundaries
and streamed into four GeoJSONL files under `OUTPUT_DIR/built-up/`. Tilemaker
unions the pieces again before simplification, removing internal chunk edges.
This avoids a continent-wide GEOS union and shapefile size limits.

`BUILT_UP_WORKERS` defaults to the smaller of `THREADS` and 16. Work in flight is
bounded to twice the worker count; output is written in a stable cell order.
The same setting controls polygon indexing and closing, which run sequentially.
Closing refills the worker queue whenever any cell finishes. Out-of-order results
wait in temporary files, so a slow cell does not leave the other workers idle.
Output is still appended in the original cell order. Allow additional scratch
space for these buffered results; in the worst case they can accumulate most of
the remaining mask data behind one slow cell. They are removed after writing
or failure and are not resumable checkpoints.
Closing progress distinguishes `completed` computation from `written` cells,
with `buffered_cells` and `in_flight` counts to explain any output backlog.
Indexing keeps one SQLite writer with a 64 MiB page cache and at most twice the
worker count in flight (roughly 4 MiB of source text per batch, except unusually
large individual features). Polygon order and IDs are preserved.
The cloud example explicitly uses 16 workers. `built_up.indexing` (including input-byte progress),
`built_up.running` and `built_up.progress` events report progress at roughly
60-second intervals; completion records elapsed time and cell/file counts.
Native filtering and export have their own stage logs.
Polygons are checked for validity after projection, closing and inverse
projection, and repaired with GEOS `make_valid` when needed; repairs log their
stage, zoom and bounds. Valid source geometry can still become invalid after
coordinate transformations. Failed cells are logged immediately as
`built_up.cell_failed`; queued jobs are cancelled, and `built_up.stopping`
reports any workers still draining before the pipeline exits.

Completed masks are published together with a manifest and reused when their
source, code and dependency records match. A failed preparation restarts the
mask step; it does not resume individual cells. Temporary filtered data and the
spatial index are deleted on success. Allow scratch space for these intermediates
in `OUTPUT_DIR`, as well as the persistent GeoJSONL files. The local preview
checks correctness; it does not establish full-Europe runtime or peak storage.

Paved pedestrian polygons are also included in the z6–9 mask, accepting both
`highway=pedestrian` + `area=yes` and `area:highway=pedestrian`. Only explicitly
hard-surfaced areas qualify; covered and underground areas are excluded.
At z10+ these surfaces are emitted as `transportation/class=path`,
`subclass=pedestrian` polygons, without an area-based delay at the handover.
The detailed style needs a transportation polygon fill (the existing broad
`road_area` fill works). Unpaved/unspecified surfaces retain their detailed
transportation behavior at z14. No pedestrian labels are added.

`landuse=quarry` is emitted separately as `landuse/class=quarry`, using the
existing area thresholds: large sites can appear at z6/7, smaller ones later.
Quarries retain their outlines and are excluded from settlement closing. Add
`quarry` to a style filter or give it its own fill; it is not part of `built_up`.

Complete normal OSM landuse class selection:

| Original classes | Overview z6–9 | Detail |
| --- | --- | --- |
| `residential`, `commercial`, `industrial`, `retail` | Combined `built_up` | Original classes from z10 |
| `railway`, `bus_station` | Combined `built_up` | Original classes from z10; land areas, not railway lines |
| `school`, `university`, `college`, `kindergarten`, `library`, `hospital` | Combined `built_up` | Original classes from z10; draw separate campus greenery above the fill |
| `quarry` | Separate `quarry`, according to area | Same class and area thresholds; no closing |
| `cemetery`, `pitch`, `playground` | Excluded | Original classes from z11; keep green or style separately |
| `military`, `stadium`, `theme_park`, `zoo` | Excluded | Original classes from z11; mixed grounds often contain open space |

Styles must include `built_up` alongside the twelve original bright-fill classes
in their `landuse` filter. Filling every landuse class also paints the seven
excluded open/mixed uses as urban land. Natural Earth still supplies generalized
`residential` shapes at z4–5; there is no urban fill below z4.

Special `waterway=boatyard` and `waterway=fuel` mappings retain their `industrial`
class at z12 and z14 respectively. This list describes this Lua mapping, not all
possible OSM tags: `brownfield`, for example, is not emitted as a landuse polygon.
Farmland is `landcover/class=farmland`, not `landuse/class=agriculture`.
Forests, grass, parks, gardens and allotments also go through `landcover`.

Run a small Cologne comparison before rebuilding Europe:

```bash
uv run tiles preview --before tilesets/nrw-v4.mbtiles \
  --directory /tmp/cologne-closing-preview --bbox 6.8,50.85,7.1,51.02 \
  --landuse built-up
```

The default `built-up` preview compares the mask, original bright-fill classes
and pedestrian transportation polygons at z6–11, including the z9→10 handover. `--landuse residential` only shows original
residential polygons (z6–9); the combined candidate mask cannot be separated into
residential polygons anymore and is therefore omitted in that diagnostic.
The HTML enlarges selected polygons to the same extent; also review `after.mbtiles`
in the actual map style. Cropped previews must not replace regional tilesets.

After updating the server checkout, keep its existing input/thread settings in
`.env`, sync the locked dependencies, and use a fresh output directory:

```bash
uv sync --locked
OUTPUT_DIR=tilesets/closing-candidate uv run tiles plan
OUTPUT_DIR=tilesets/closing-candidate uv run tiles run
```

Use a new output directory: Lua/config fingerprints changed and existing outputs
intentionally fail the stale-output check. Prepared inputs can be reused when
their provenance matches. The final file is still named
`tilesets/closing-candidate/nrw-v4.mbtiles`; review before replacing the served file.
The overview change applies throughout Europe. DACH/NRW at z13–14 retain original
classes. The app's existing `built_up` filter remains compatible; label settings
are unchanged.
