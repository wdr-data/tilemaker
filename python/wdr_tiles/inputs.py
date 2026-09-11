"""Download dated Geofabrik extracts and prepare compact Tilemaker inputs."""

import shutil
from collections.abc import Mapping, Sequence
from pathlib import Path

from .logging import log
from .settings import Settings
from .setup import available, download, static_data
from .state import digest, file_record, publish, reusable, run_command, workspace

GEOFABRIK = {
    "europe": "https://download.geofabrik.de/europe",
    "dach": "https://download.geofabrik.de/europe/dach",
}


def renumber_record(
    settings: Settings, profile: str, raw: Path, checksum: str
) -> Mapping[str, object]:
    osmium = shutil.which(settings.osmium)
    if osmium is None:
        raise ValueError(f"Missing executable: {settings.osmium}")
    return {
        "version": 1,
        "step": "renumber-geofabrik",
        "profile": profile,
        "snapshot": settings.geofabrik_date,
        "input": file_record(raw),
        "input_md5": checksum,
        "osmium_sha256": digest(osmium),
        "output_sha256": settings.checksum(profile),
    }


def prepare_geofabrik(
    settings: Settings, profiles: Sequence[str] = ("europe", "dach")
) -> None:
    """Keep originals and publish renumbered files with records for later reuse."""
    if not settings.geofabrik_date:
        raise ValueError("Set GEOFABRIK_DATE=YYMMDD first.")
    for tool in (settings.osmium, "curl"):
        if not available(tool):
            raise ValueError(f"Missing {tool}; run `uv run tiles setup` first.")

    for profile in profiles:
        destination = settings.input(profile)
        filename = f"{profile}-{settings.geofabrik_date}.osm.pbf"
        raw = destination.parent / filename
        if raw == destination:
            raise ValueError(
                f"{profile.upper()}_INPUT must differ from the original download path {raw}"
            )
        url = f"{GEOFABRIK[profile]}-{settings.geofabrik_date}.osm.pbf"
        checksum_file = Path(str(raw) + ".md5")
        download(settings, checksum_file, url + ".md5")
        fields = checksum_file.read_text().split()
        checksum = fields[0].lower() if fields else ""
        if len(checksum) != 32 or any(c not in "0123456789abcdef" for c in checksum):
            raise ValueError(f"Invalid Geofabrik checksum file: {checksum_file}")

        if destination.exists():
            # Identity + record check avoids hashing the large raw PBF on each rerun.
            # Keep originals and sidecars; do not silently trust an unstamped output.
            reusable(destination, renumber_record(settings, profile, raw, checksum))
            continue

        download(settings, raw, url, checksum, algorithm="md5")
        expected = renumber_record(settings, profile, raw, checksum)
        with workspace(destination) as work:
            staged = work / "renumbered.osm.pbf"
            run_command(
                settings,
                f"renumber-{profile}",
                [
                    settings.osmium,
                    "renumber",
                    raw,
                    "--output",
                    staged,
                ],
            )
            if renumber_record(settings, profile, raw, checksum) != expected:
                raise ValueError(
                    f"{raw} changed during renumbering; output was not published."
                )
            output_checksum = settings.checksum(profile)
            if output_checksum and digest(staged).lower() != output_checksum.lower():
                raise ValueError(
                    f"SHA-256 mismatch for renumbered {profile}; output was not published."
                )
            publish(staged, destination, expected)
        log.info("input.prepared", path=str(destination))


def source_data(settings: Settings) -> None:
    for profile in ("europe", "dach"):
        path = settings.input(profile)
        checksum = settings.checksum(profile)
        if settings.geofabrik_date:
            prepare_geofabrik(settings, (profile,))
        else:
            if not path.is_file():
                raise ValueError(
                    f"Missing renumbered input: {path}. Supply it manually, or set "
                    "GEOFABRIK_DATE and run `uv run tiles prepare-inputs`."
                )
            if checksum and digest(path).lower() != checksum.lower():
                raise ValueError(f"SHA-256 mismatch: {path}")
        run_command(settings, f"check-{path.name}", [settings.osmium, "fileinfo", path])
    static_data(settings)
