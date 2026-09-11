"""Validated settings: shell variables override .env, then field defaults."""

import os
import shutil
from datetime import datetime
from pathlib import Path
from typing import Self, cast

from pydantic import Field, ValidationInfo, field_validator
from pydantic_core import PydanticUseDefault
from pydantic_settings import BaseSettings, SettingsConfigDict

PROFILES = ("coastline", "europe", "dach", "nrw")
NRW_BOUNDS = "4.21,48.51,11.77,53.44"
EXTRACT_BOUNDS = "4.1,48.4,11.9,53.55"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        extra="ignore",
        frozen=True,
        populate_by_name=True,
        case_sensitive=False,
        env_ignore_empty=False,
        hide_input_in_errors=True,
    )

    # Field order matters: path defaults/validators use root and the snapshot.
    root: Path = Field(
        default_factory=lambda: Path(__file__).resolve().parents[2],
        validation_alias="PROJECT_ROOT",
    )
    geofabrik_date: str = Field("", validation_alias="GEOFABRIK_DATE")
    output_dir: Path = Field(
        Path("tilesets/v4-candidate"), validation_alias="OUTPUT_DIR"
    )
    europe: Path = Field(
        default_factory=lambda data: Path(
            f"osm/europe-{data['geofabrik_date'] or '260910'}-renumbered.osm.pbf"
        ),
        validation_alias="EUROPE_INPUT",
    )
    dach: Path = Field(
        default_factory=lambda data: Path(
            f"osm/dach-{data['geofabrik_date'] or '260910'}-renumbered.osm.pbf"
        ),
        validation_alias="DACH_INPUT",
    )
    nrw: Path = Field(
        default_factory=lambda data: Path(
            f"osm/nrw-buffer-{data['geofabrik_date']}-renumbered.osm.pbf"
            if data["geofabrik_date"]
            else "osm/nrw-buffer-renumbered.osm.pbf"
        ),
        validation_alias="NRW_INPUT",
    )
    threads: int = Field(
        default_factory=lambda: min(os.cpu_count() or 4, 8),
        ge=1,
        validation_alias="THREADS",
    )
    compile_jobs: int = Field(
        default_factory=lambda: min(os.cpu_count() or 4, 16),
        ge=1,
        validation_alias="COMPILE_JOBS",
    )
    fast: bool = Field(False, validation_alias="FAST")
    store: Path | None = Field(None, validation_alias="STORE_DIR")
    install_system_packages: bool = Field(
        False, validation_alias="INSTALL_SYSTEM_PACKAGES"
    )
    tilemaker: str = Field("", validation_alias="TILEMAKER")
    tile_join: str = Field("", validation_alias="TILE_JOIN")
    tile_decode: str = Field("", validation_alias="TILE_DECODE")
    osmium: str = Field("", validation_alias="OSMIUM")
    europe_sha256: str = Field("", validation_alias="EUROPE_SHA256")
    dach_sha256: str = Field("", validation_alias="DACH_SHA256")

    @classmethod
    def load(cls, root: Path, env_file: Path | None = None) -> Self:
        return cls(root=root.resolve(), _env_file=env_file or root / ".env")

    @field_validator(
        "output_dir",
        "europe",
        "dach",
        "nrw",
        "threads",
        "compile_jobs",
        "fast",
        "store",
        "install_system_packages",
        mode="before",
    )
    @classmethod
    def empty_uses_default(cls, value: object) -> object:
        # An explicitly empty shell value overrides .env, including STORE_DIR.
        if value == "":
            raise PydanticUseDefault()
        return value

    @field_validator("geofabrik_date")
    @classmethod
    def valid_snapshot(cls, value: str) -> str:
        if value:
            if len(value) != 6 or not value.isascii() or not value.isdigit():
                raise ValueError("GEOFABRIK_DATE must be a date in YYMMDD format")
            try:
                datetime.strptime(value, "%y%m%d")
            except ValueError:
                raise ValueError(
                    "GEOFABRIK_DATE must be a valid date in YYMMDD format"
                ) from None
        return value

    @field_validator("root")
    @classmethod
    def absolute_root(cls, value: Path) -> Path:
        return value.expanduser().resolve()

    @field_validator("output_dir", "europe", "dach", "nrw", "store")
    @classmethod
    def resolve_path(cls, value: Path | None, info: ValidationInfo) -> Path | None:
        if value is None:
            return None
        root = cast(Path, info.data["root"])
        return (root / value.expanduser()).resolve()

    @field_validator("tilemaker", "tile_join", "tile_decode", "osmium")
    @classmethod
    def resolve_executable(cls, value: str, info: ValidationInfo) -> str:
        root = cast(Path, info.data["root"])
        if value:
            return (
                str((root / Path(value).expanduser()).resolve())
                if "/" in value
                else value
            )
        name, fallback = {
            "tilemaker": ("tilemaker", "tilemaker"),
            "tile_join": ("tile-join", ".tools/tippecanoe/tile-join"),
            "tile_decode": ("tippecanoe-decode", ".tools/tippecanoe/tippecanoe-decode"),
            "osmium": ("osmium", ".tools/bin/osmium"),
        }[cast(str, info.field_name)]
        return shutil.which(name) or str(root / fallback)

    @field_validator("europe_sha256", "dach_sha256")
    @classmethod
    def valid_checksum(cls, value: str) -> str:
        if value and (
            len(value) != 64 or any(c not in "0123456789abcdefABCDEF" for c in value)
        ):
            raise ValueError(
                "Expected an empty value or a 64-character SHA-256 checksum"
            )
        return value.lower()

    def config(self, profile: str) -> Path:
        name = (
            "config-coastline.json"
            if profile == "coastline"
            else f"config-openmaptiles-{profile}.json"
        )
        return self.root / "resources" / name

    def process(self, profile: str) -> Path:
        name = (
            "process-coastline.lua"
            if profile == "coastline"
            else "process-openmaptiles.lua"
        )
        return self.root / "resources" / name

    def input(self, profile: str) -> Path:
        return {"europe": self.europe, "dach": self.dach, "nrw": self.nrw}[profile]

    def output(self, profile: str) -> Path:
        return self.output_dir / f"{profile}.mbtiles"

    def checksum(self, profile: str) -> str:
        return {"europe": self.europe_sha256, "dach": self.dach_sha256}[profile]
