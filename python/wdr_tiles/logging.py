"""Readable console events and an append-only JSON event log for every CLI run."""

import logging
import sys
import time
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

import structlog
from structlog.stdlib import BoundLogger, ProcessorFormatter

from .settings import Settings

logging.getLogger("wdr_tiles").addHandler(logging.NullHandler())
log: BoundLogger = structlog.get_logger("wdr_tiles")


def safe_command(args: Sequence[str]) -> list[str]:
    """Retain useful argv while omitting credentials/query strings in URLs."""
    result = []
    for arg in args:
        if arg.startswith(("http://", "https://")):
            url = urlsplit(arg)
            arg = urlunsplit(
                (
                    url.scheme,
                    url.netloc.rsplit("@", 1)[-1],
                    url.path,
                    "redacted" if url.query else "",
                    "",
                )
            )
        result.append(arg)
    return result


@contextmanager
def log_session(settings: Settings, command: str) -> Iterator[None]:
    directory = settings.output_dir / "logs"
    directory.mkdir(parents=True, exist_ok=True)
    event_file = directory / "pipeline.jsonl"
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.stdlib.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.format_exc_info,
            ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=BoundLogger,
        cache_logger_on_first_use=False,
    )
    logger = logging.getLogger("wdr_tiles")
    old_handlers, old_level, old_propagate = (
        logger.handlers[:],
        logger.level,
        logger.propagate,
    )
    console = logging.StreamHandler(sys.stderr)
    console.setFormatter(
        ProcessorFormatter(
            processors=[
                ProcessorFormatter.remove_processors_meta,
                structlog.dev.ConsoleRenderer(colors=sys.stderr.isatty()),
            ]
        )
    )
    file = logging.FileHandler(event_file, encoding="utf-8")
    file.setFormatter(
        ProcessorFormatter(
            processors=[
                ProcessorFormatter.remove_processors_meta,
                structlog.processors.JSONRenderer(sort_keys=True),
            ]
        )
    )
    logger.handlers = [console, file]
    logger.setLevel(logging.INFO)
    logger.propagate = False
    started = time.monotonic()
    try:
        with structlog.contextvars.bound_contextvars(
            run_id=uuid4().hex, command=command
        ):
            log.info(
                "pipeline.started",
                event_log=str(event_file),
                settings=settings.model_dump(mode="json"),
            )
            try:
                yield
            except KeyboardInterrupt:
                log.warning(
                    "pipeline.interrupted",
                    elapsed_seconds=round(time.monotonic() - started, 3),
                )
                raise
            except Exception:
                log.exception(
                    "pipeline.failed",
                    elapsed_seconds=round(time.monotonic() - started, 3),
                )
                raise
            else:
                log.info(
                    "pipeline.completed",
                    elapsed_seconds=round(time.monotonic() - started, 3),
                )
    finally:
        logger.handlers, logger.level, logger.propagate = (
            old_handlers,
            old_level,
            old_propagate,
        )
        console.close()
        file.close()
