"""Ordered batch CLI; individual failures never discard successful records."""

import argparse
import logging
import sqlite3
from pathlib import Path

from . import __version__
from .config import ConfigError, Settings, load_prompts
from .exif import InvalidJPEG, is_jpeg_path
from .geonames.database import GeoNamesDatabase
from .geonames.reverse import ReverseGeocoder
from .llm.base import LLMError
from .llm.openai_compatible import OpenAICompatibleAnalyzer
from .models import BatchResult
from .output import write_yaml
from .pipeline import Pipeline

LOG = logging.getLogger(__name__)


def process_batch(paths: list[Path], pipeline: Pipeline) -> BatchResult:
    images = []
    skipped = failed = 0
    for path in paths:
        if not is_jpeg_path(path):
            LOG.warning("skipping non-JPEG file: %s", path)
            skipped += 1
            continue
        LOG.info("processing %s", path)
        try:
            images.append(pipeline.process(path))
        except (OSError, InvalidJPEG):
            LOG.warning("skipping unreadable JPEG: %s", path)
            skipped += 1
        except LLMError as exc:
            LOG.error("analysis failed for %s: %s", path, exc)
            failed += 1
        except Exception as exc:
            # Third-party exception messages may contain requests or credentials.
            LOG.error("processing failed for %s (%s)", path, type(exc).__name__)
            failed += 1
    LOG.debug("batch summary: %d succeeded, %d skipped, %d failed", len(images), skipped, failed)
    return BatchResult(images=images)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="lenzcontext", description="Analyze JPEG images into one YAML file.")
    parser.add_argument("-v", "--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("-V", "--verbose", action="store_true", help="enable DEBUG diagnostics on stderr")
    parser.add_argument("images", nargs="+", type=Path)
    parser.add_argument("-o", "--output", type=Path, default=Path("lenzcontext.yaml"))
    parser.add_argument("--geonames-db", type=Path, default=Path("data/geonames.db"))
    parser.add_argument("--prompt-config", type=Path, default=None, help="default: config/prompts.yaml")
    parser.add_argument("--structured-output", action="store_true", help="request JSON Schema, falling back on rejection")
    parser.add_argument("--timeout", type=float, default=120, help="API timeout in seconds (default: 120)")
    args = parser.parse_args(argv)
    fmt = "%(levelname)s: %(name)s: %(message)s" if args.verbose else "%(levelname)s: %(message)s"
    logging.basicConfig(level=logging.INFO, format=fmt)
    # Pillow dumps raw EXIF tags at DEBUG; keep that out of CLI output.
    logging.getLogger("PIL").setLevel(logging.WARNING)
    if args.verbose:
        logging.getLogger("lenzcontext").setLevel(logging.DEBUG)
    else:
        logging.getLogger("lenzcontext").setLevel(logging.INFO)
    LOG.debug("inputs=%d geonames_db=%s prompt_config=%s output=%s structured_output=%s timeout=%s",
              len(args.images), args.geonames_db, args.prompt_config or "<auto>",
              args.output, args.structured_output, args.timeout)
    if any(args.output.resolve() == image.resolve() for image in args.images):
        LOG.error("output must not overwrite an input image")
        return 2
    if args.output.resolve() == args.geonames_db.resolve():
        LOG.error("output must not overwrite the GeoNames database")
        return 2
    try:
        settings = Settings.from_env(structured_output=args.structured_output, timeout=args.timeout)
        prompts = load_prompts(args.prompt_config)
    except ConfigError as exc:
        LOG.error("configuration error: %s", exc)
        return 2
    database = None
    geocoder = None
    try:
        try:
            database = GeoNamesDatabase(args.geonames_db)
            geocoder = ReverseGeocoder(database)
        except (OSError, sqlite3.Error):
            LOG.warning("GeoNames database unavailable; continuing without addresses (run the importer)")
        pipeline = Pipeline(OpenAICompatibleAnalyzer(settings, prompts), geocoder)
        batch = process_batch(args.images, pipeline)
        if not batch.images:
            LOG.error("no JPEG images were successfully processed; output was not written")
            return 1
        try:
            write_yaml(batch, args.output)
        except OSError:
            LOG.error("could not write output: %s", args.output)
            return 1
        LOG.info("wrote %d image records to %s", len(batch.images), args.output)
        return 0
    finally:
        if database is not None:
            database.close()
