"""Prepare native executables and source data for an Ubuntu/Debian build host."""

import os
import shutil
from pathlib import Path
from urllib.parse import urlparse
from zipfile import ZipFile

from .logging import log
from .settings import Settings
from .state import digest, run_command, workspace

# The revision used for the local preview and merge checks. Do not follow master.
TIPPECANOE_REVISION = "d7b2892f98011f12b460359b4879d5ba378d5321"
PACKAGES = [
    "build-essential",
    "git",
    "curl",
    "ca-certificates",
    "osmium-tool",
    "libboost-dev",
    "libboost-filesystem-dev",
    "libboost-program-options-dev",
    "libboost-system-dev",
    "lua5.1",
    "liblua5.1-0-dev",
    "libshp-dev",
    "libsqlite3-dev",
    "rapidjson-dev",
    "zlib1g-dev",
]
SHAPEFILES = [
    (
        "coastline/water_polygons",
        "https://osmdata.openstreetmap.de/download/water-polygons-split-4326.zip",
    ),
    (
        "landcover/ne_10m_urban_areas/ne_10m_urban_areas",
        "https://naciscdn.org/naturalearth/10m/cultural/ne_10m_urban_areas.zip",
    ),
    (
        "landcover/ne_10m_antarctic_ice_shelves_polys/ne_10m_antarctic_ice_shelves_polys",
        "https://naciscdn.org/naturalearth/10m/physical/ne_10m_antarctic_ice_shelves_polys.zip",
    ),
    (
        "landcover/ne_10m_glaciated_areas/ne_10m_glaciated_areas",
        "https://naciscdn.org/naturalearth/10m/physical/ne_10m_glaciated_areas.zip",
    ),
]
SHAPE_EXTENSIONS = (".shp", ".shx", ".dbf", ".prj")


def available(command: str) -> bool:
    return shutil.which(command) is not None


def native_tools(settings: Settings) -> Settings:
    """Install missing OS dependencies when opted in, then build local tools."""
    required = (
        settings.tilemaker,
        settings.tile_join,
        settings.tile_decode,
        settings.osmium,
        "curl",
    )
    if all(available(tool) for tool in required):
        return settings

    if settings.install_system_packages:
        if not available("apt-get"):
            raise ValueError("Automatic native setup supports Ubuntu/Debian (apt-get).")
        prefix = [] if os.geteuid() == 0 else ["sudo", "-n"]
        run_command(settings, "apt-update", [*prefix, "apt-get", "update"])
        run_command(
            settings, "apt-install", [*prefix, "apt-get", "install", "-y", *PACKAGES]
        )

    if not available(settings.tilemaker):
        if settings.tilemaker != str(settings.root / "tilemaker"):
            raise ValueError(f"Configured TILEMAKER is missing: {settings.tilemaker}")
        run_command(
            settings,
            "compile-tilemaker",
            ["make", f"-j{settings.compile_jobs}", "tilemaker"],
        )

    if not available(settings.tile_join) or not available(settings.tile_decode):
        directory = settings.root / ".tools/tippecanoe"
        defaults = (str(directory / "tile-join"), str(directory / "tippecanoe-decode"))
        for configured, default in zip(
            (settings.tile_join, settings.tile_decode), defaults, strict=True
        ):
            if not available(configured) and configured != default:
                raise ValueError(f"Configured executable is missing: {configured}")
        directory.mkdir(parents=True, exist_ok=True)
        if not (directory / ".git").exists():
            run_command(settings, "tippecanoe-init", ["git", "init", directory])
        run_command(
            settings,
            "tippecanoe-fetch",
            [
                "git",
                "-C",
                directory,
                "fetch",
                "--depth=1",
                "https://github.com/felt/tippecanoe.git",
                TIPPECANOE_REVISION,
            ],
        )
        run_command(
            settings,
            "tippecanoe-checkout",
            ["git", "-C", directory, "checkout", "--detach", "FETCH_HEAD"],
        )
        run_command(
            settings,
            "compile-tippecanoe",
            [
                "make",
                "-C",
                directory,
                f"-j{settings.compile_jobs}",
                "tile-join",
                "tippecanoe-decode",
            ],
        )

    # apt installs osmium on PATH, not into .tools.
    installed_osmium = shutil.which("osmium")
    if settings.osmium == str(settings.root / ".tools/bin/osmium") and installed_osmium:
        settings = settings.model_copy(update={"osmium": installed_osmium})
    for tool in (
        settings.tilemaker,
        settings.tile_join,
        settings.tile_decode,
        settings.osmium,
        "curl",
    ):
        if not available(tool):
            raise ValueError(
                f"Missing executable: {tool}. Install native dependencies or set INSTALL_SYSTEM_PACKAGES=true."
            )
    return settings


def download(
    settings: Settings,
    destination: Path,
    url: str,
    checksum: str = "",
    algorithm: str = "sha256",
) -> None:
    """Resume a .part download; publish only after curl and optional checksum pass."""
    if algorithm not in ("sha256", "md5"):
        raise ValueError(f"Unsupported checksum algorithm: {algorithm}")
    checksum_name = "SHA-256" if algorithm == "sha256" else "MD5"
    checksum_length = 64 if algorithm == "sha256" else 32
    if checksum and (
        len(checksum) != checksum_length
        or any(c not in "0123456789abcdefABCDEF" for c in checksum)
    ):
        raise ValueError(f"Invalid {checksum_name} configured for {destination.name}")
    if destination.exists():
        if checksum and digest(destination, algorithm).lower() != checksum.lower():
            raise ValueError(f"{checksum_name} mismatch: {destination}")
        return
    if not url:
        raise ValueError(f"Missing {destination}; no download URL was provided.")
    if urlparse(url).scheme not in ("http", "https"):
        raise ValueError(f"The download URL for {destination.name} must use HTTP(S).")
    destination.parent.mkdir(parents=True, exist_ok=True)
    partial = Path(str(destination) + ".part")
    run_command(
        settings,
        f"download-{destination.name}",
        [
            "curl",
            "--fail",
            "--location",
            "--retry",
            "5",
            "--continue-at",
            "-",
            "--output",
            partial,
            "--url",
            url,
        ],
    )
    if partial.stat().st_size == 0:
        raise ValueError(f"Empty download: {partial}")
    if checksum and digest(partial, algorithm).lower() != checksum.lower():
        raise ValueError(
            f"{checksum_name} mismatch: {partial}; remove it before retrying."
        )
    os.link(partial, destination)
    partial.unlink()


def static_data(settings: Settings) -> None:
    for relative, url in SHAPEFILES:
        base = settings.root / relative
        expected = [base.with_suffix(ext) for ext in SHAPE_EXTENSIONS]
        if all(path.is_file() for path in expected):
            continue
        archive = settings.root / ".tools/downloads" / url.rsplit("/", 1)[-1]
        download(settings, archive, url)
        with workspace(base) as work, ZipFile(archive) as zipfile:
            # Match only the named components; never extract paths from the archive.
            for destination in expected:
                matches = [
                    name
                    for name in zipfile.namelist()
                    if Path(name).name == destination.name
                ]
                if len(matches) != 1:
                    raise ValueError(
                        f"{archive} does not contain one {destination.name}"
                    )
                staged = work / destination.name
                with zipfile.open(matches[0]) as source, staged.open("wb") as target:
                    shutil.copyfileobj(source, target)
            for destination in expected:
                os.replace(work / destination.name, destination)
        log.info("shapefile.prepared", path=str(base))
