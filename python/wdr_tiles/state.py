"""Publish completed files and refuse to reuse outputs from different inputs."""

import fcntl
import hashlib
import json
import os
import signal
import sqlite3
import subprocess
import tempfile
import time
from collections.abc import Iterator, Mapping, Sequence
from contextlib import closing, contextmanager
from pathlib import Path
from typing import TypedDict

from structlog.contextvars import get_contextvars

from .logging import log, safe_command
from .settings import Settings

HEARTBEAT_SECONDS = 60
KEY = "wdr:build"
CommandArg = str | Path | int


class FileRecord(TypedDict):
    path: str
    bytes: int
    mtime_ns: int


def digest(path: str | Path, algorithm: str = "sha256") -> str:
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, algorithm).hexdigest()


def file_record(path: str | Path) -> FileRecord:
    path = Path(path).resolve()
    stat = path.stat()
    return {"path": str(path), "bytes": stat.st_size, "mtime_ns": stat.st_mtime_ns}


def reusable(path: Path, expected: Mapping[str, object]) -> bool:
    if not path.exists():
        return False
    try:
        if path.suffix == ".mbtiles":
            with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as db:
                row = db.execute(
                    "SELECT value FROM metadata WHERE name=?", (KEY,)
                ).fetchone()
                actual = json.loads(row[0]) if row else None
        else:
            saved = json.loads(Path(str(path) + ".build.json").read_text())
            actual = saved["build"] if saved["output"] == file_record(path) else None
    except (OSError, ValueError, KeyError, sqlite3.Error):
        actual = None
    if actual != expected:
        log.error(
            "output.stale", path=str(path), expected=dict(expected), actual=actual
        )
        raise ValueError(
            f"{path} exists but its build record differs or is missing. "
            "Choose new output/input paths for changed data, "
            "or move the old file aside deliberately."
        )
    log.info("output.reused", path=str(path))
    return True


def publish(temporary: Path, destination: Path, record: Mapping[str, object]) -> None:
    if destination.suffix == ".mbtiles":
        with closing(sqlite3.connect(temporary)) as db, db:
            db.execute("DELETE FROM metadata WHERE name=?", (KEY,))
            db.execute(
                "INSERT INTO metadata VALUES (?, ?)",
                (KEY, json.dumps(record, sort_keys=True)),
            )
    # Both files are on the same filesystem. A concurrent writer cannot be overwritten.
    os.link(temporary, destination)
    if destination.suffix != ".mbtiles":
        sidecar = Path(str(destination) + ".build.json")
        staged = temporary.parent / "build.json"
        staged.write_text(
            json.dumps({"build": record, "output": file_record(destination)}, indent=2)
        )
        os.replace(staged, sidecar)
    log.info(
        "output.published", path=str(destination), bytes=destination.stat().st_size
    )


@contextmanager
def workspace(destination: Path) -> Iterator[Path]:
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix=f".{destination.stem}-", dir=destination.parent
    ) as directory:
        yield Path(directory)


@contextmanager
def pipeline_lock(output_dir: Path) -> Iterator[None]:
    output_dir.mkdir(parents=True, exist_ok=True)
    with (output_dir / ".pipeline.lock").open("a") as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError(f"Another pipeline is using {output_dir}") from None
        yield


def run_command(settings: Settings, label: str, args: Sequence[CommandArg]) -> None:
    """Keep raw native output, with command/exit details and periodic status events."""
    logs = settings.output_dir / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    native_log = logs / f"{label}.log"
    argv = [str(arg) for arg in args]
    stage_log = log.bind(stage=label, native_log=str(native_log))
    started = time.monotonic()
    env = {**os.environ, "TIPPECANOE_MAX_THREADS": str(settings.threads)}
    stage_log.info("native.started", argv=safe_command(argv), cwd=str(settings.root))
    try:
        with native_log.open("a") as stream:
            run_id = get_contextvars().get("run_id", "standalone")
            stream.write(
                f"\n--- {time.strftime('%Y-%m-%d %H:%M:%S')} {label} run_id={run_id} ---\n"
            )
            stream.flush()
            with subprocess.Popen(
                argv,
                cwd=settings.root,
                env=env,
                stdout=stream,
                stderr=subprocess.STDOUT,
            ) as process:
                try:
                    while True:
                        try:
                            code = process.wait(timeout=HEARTBEAT_SECONDS)
                            break
                        except subprocess.TimeoutExpired:
                            stage_log.info(
                                "native.running",
                                pid=process.pid,
                                elapsed_seconds=round(time.monotonic() - started, 3),
                            )
                except BaseException:
                    # Stop the child before callers clean up its temporary files.
                    process.terminate()
                    try:
                        process.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait()
                    raise
    except (OSError, KeyboardInterrupt):
        stage_log.exception(
            "native.interrupted_or_unavailable",
            elapsed_seconds=round(time.monotonic() - started, 3),
        )
        raise
    elapsed = round(time.monotonic() - started, 3)
    if code:
        signal_name = None
        if code < 0:
            try:
                signal_name = signal.Signals(-code).name
            except ValueError:
                signal_name = str(-code)
        stage_log.error(
            "native.failed",
            returncode=code,
            signal=signal_name,
            elapsed_seconds=elapsed,
        )
        raise ValueError(f"{label} failed (exit {code}); see {native_log}")
    stage_log.info("native.completed", returncode=code, elapsed_seconds=elapsed)
