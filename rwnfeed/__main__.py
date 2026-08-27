"""CLI entrypoint. Runs on a cron schedule inside the container, or once."""

from __future__ import annotations

import argparse
import logging
import signal
import sys
import time
from dataclasses import replace
from datetime import datetime, timezone

from .config import AppConfig, ConfigError
from .run import run_once

log = logging.getLogger("rwnfeed")

_stopping = False


def _handle_signal(signum, _frame):
    global _stopping
    _stopping = True
    log.info("received signal %s, shutting down after the current run", signum)


def configure_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S%z",
        stream=sys.stdout,
    )
    logging.getLogger("botocore").setLevel(logging.WARNING)
    logging.getLogger("boto3").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="rwnfeed",
        description="Mirror unlocked Patreon episodes to Cloudflare R2 as a podcast feed.",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="run a single pass and exit instead of following CRON_SCHEDULE",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="list what would be mirrored without downloading or uploading",
    )
    parser.add_argument(
        "--schedule",
        help="cron expression to follow (overrides CRON_SCHEDULE)",
    )
    parser.add_argument("--log-level", help="DEBUG, INFO, WARNING, ERROR")
    return parser.parse_args(argv)


def _sleep_until(target: datetime) -> None:
    """Sleep in short slices so signals are handled promptly."""
    while not _stopping:
        remaining = (target - datetime.now(timezone.utc)).total_seconds()
        if remaining <= 0:
            return
        time.sleep(min(remaining, 30))


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    try:
        config = AppConfig.from_env()
    except ConfigError as exc:
        configure_logging("INFO")
        log.error("configuration error: %s", exc)
        return 2

    overrides = {}
    if args.dry_run:
        overrides["dry_run"] = True
    if args.schedule:
        overrides["schedule"] = args.schedule
    if args.log_level:
        overrides["log_level"] = args.log_level.upper()
    if overrides:
        config = replace(config, **overrides)

    configure_logging(config.log_level)
    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    single = args.once or config.run_once
    if single:
        try:
            run_once(config)
        except Exception as exc:  # noqa: BLE001 - report and exit non-zero
            log.exception("run failed: %s", exc)
            return 1
        return 0

    from croniter import croniter

    if not croniter.is_valid(config.schedule):
        log.error("invalid CRON_SCHEDULE %r", config.schedule)
        return 2

    log.info("scheduler started with CRON_SCHEDULE=%r", config.schedule)
    schedule = croniter(config.schedule, datetime.now(timezone.utc))
    while not _stopping:
        next_run = schedule.get_next(datetime)
        log.info("next run at %s", next_run.isoformat())
        _sleep_until(next_run)
        if _stopping:
            break
        try:
            run_once(config)
        except Exception as exc:  # noqa: BLE001 - keep the scheduler alive
            log.exception("run failed, will try again on the next tick: %s", exc)
    log.info("scheduler stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
