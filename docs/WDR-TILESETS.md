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
tail -f tilesets/v4-candidate/logs/pipeline.jsonl
tail -f tilesets/v4-candidate/logs/build-europe.log
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
shapefile identities. PBFs generated by this pipeline have `.build.json` sidecars; keep them with
the files. Merge validates all four builds. Changed inputs or unstamped old outputs
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
uv run tiles build europe
uv run tiles build dach
uv run tiles build nrw
uv run tiles merge
```

Individual steps assume their prerequisites are ready. Use the Python commands
above. Settings are listed in `.env.example`.

The code is split by responsibility: `cli.py` wires commands, `settings.py`
loads settings, `pipeline.py` lists the steps, `setup.py` prepares tools/static data, `inputs.py` downloads and renumbers OSM,
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
files, all four tilesets, filtered merge inputs, and the final merged file at
once. No full-Europe runtime or peak-storage benchmark has been measured for
this pipeline; do not size storage from the final MBTiles alone.

## Coverage and ownership

| Input | Zooms | Purpose |
| --- | --- | --- |
| coastline | 0–12 | Global ocean/background shapes; urban outlines only at 4–5 |
| Europe | 0–12 | OSM data; individual residential patches starting at 6 |
| DACH | 13 | More detail outside the NRW buffer |
| NRW buffer | 13–14 | Highest priority wherever a tile exists |

The NRW buffer is `4.21,48.51,11.77,53.44`, not the state boundary. Extraction uses
a larger `4.1,48.4,11.9,53.55` rectangle to preserve geometry at the output edges.
At shared zooms, the higher-priority regional input owns the **whole tile**.
Lower-priority overlapping tiles are excluded before `tile-join`, leaving
originals untouched. This prevents duplicate transparent fills, labels, and
roads while keeping NRW's additional coverage outside DACH.

Static shapes occur only in coastline at z0–12. DACH and NRW supply ocean at
z13–14. Rebuild coastline as well as Europe: the old coastline file contained
Natural Earth urban polygons through z8, which caused the broad grey blankets.

Urban land-use and forest land-cover retain the same simplification:
`simplify_below: 13`, `simplify_level: 0.0003`, default `simplify_ratio: 2`.
Natural Earth urban shapes end at z5. OSM residential patches of about 0.093 km²
and larger appear at z6, patches of about 0.023 km² at z7, and smaller patches at
z8. Removing the blanket does not require finer urban boundaries than forests.

## Why the regional configs differ

The 20 OSM layer definitions are identical across Europe, DACH, and NRW.
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
The audit did not require changes to the production JSON or Lua.

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
the production Lua with the Europe config limited to z6–9. Its `comparison.html`
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
