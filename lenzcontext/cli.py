"""Ordered batch CLI; individual failures never discard successful records."""

import argparse
import logging
import os
import sqlite3
import time
from collections.abc import Callable
from pathlib import Path
from threading import Event

from . import __version__
from .batch import process_parallel
from .config import ConfigError, Settings, load_prompts
from .exif import InvalidJPEG, is_jpeg_path
from .geonames.database import GeoNamesDatabase
from .geonames.reverse import ReverseGeocoder
from .llm.base import AnalysisCancelled, LLMError
from .llm.openai_compatible import OpenAICompatibleAnalyzer
from .models import BatchResult, ImageResult
from .output import IncrementalYamlWriter, InvalidOutput
from .pipeline import Pipeline

LOG = logging.getLogger(__name__)


def recursive_jpegs(directories: list[str]) -> list[str]:
    paths = []
    for directory in directories:
        for parent, _, names in os.walk(directory, followlinks=True):
            for name in names:
                path = Path(parent) / name
                if is_jpeg_path(path):
                    paths.append(str(path))
    return paths


def process_batch(paths: list[Path | str], pipeline: Pipeline,
                  on_success: Callable[[ImageResult], None] | None = None, *,
                  jobs: int = 1, stop_event: Event | None = None) -> BatchResult:
    if type(jobs) is not int or jobs < 1:
        raise ValueError("jobs must be a positive integer")
    if jobs > 1:
        return process_parallel(paths, pipeline, on_success, jobs=jobs, stop_event=stop_event)
    images = []
    skipped = failed = 0
    for path in paths:
        if stop_event is not None and stop_event.is_set():
            raise AnalysisCancelled("batch cancelled")
        if not is_jpeg_path(Path(path)):
            LOG.warning("skipping non-JPEG file: %s", path)
            skipped += 1
            continue
        started = time.perf_counter()
        try:
            image = pipeline.process(path)
        except AnalysisCancelled:
            raise
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
        else:
            if on_success is not None:
                on_success(image)
            images.append(image)
        finally:
            LOG.info("processing %s (%.2fs)", path, time.perf_counter() - started)
    LOG.debug("batch summary: %d succeeded, %d skipped, %d failed", len(images), skipped, failed)
    return BatchResult(images=images)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="lenzcontext", description="Analyze JPEG images into one YAML file.")
    parser.add_argument("-v", "--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("-V", "--verbose", action="store_true", help="enable DEBUG diagnostics on stderr")
    parser.add_argument("images", nargs="+", help="JPEG files, or directories with -R")
    parser.add_argument("-R", "--recursive", action="store_true",
                        help="recursively find JPEG files in input directories, following symbolic links")
    output_group = parser.add_mutually_exclusive_group()
    output_group.add_argument("-o", "--output", type=Path, help="output YAML file (default: lenzcontext.yaml)")
    output_group.add_argument("-a", "--append", type=Path, metavar="FILE", help="append records to an existing YAML file")
    parser.add_argument("--geonames-db", type=Path,
                        help="GeoNames database (LENZCONTEXT_GEONAMES_DB; default: data/geonames.db)")
    parser.add_argument("--prompt-config", type=Path,
                        help="prompt file (LENZCONTEXT_PROMPT_CONFIG; default: auto-detect)")
    parser.add_argument("--description-language", metavar="LANGUAGE",
                        help="description language (LENZCONTEXT_DESCRIPTION_LANGUAGE; default: English)")
    parser.add_argument("--structured-output", action="store_true", help="request JSON Schema, falling back on rejection")
    parser.add_argument("--timeout", type=float,
                        help="API timeout in seconds (LENZCONTEXT_TIMEOUT; default: 120)")
    parser.add_argument("--retries", type=int, metavar="N",
                        help="maximum LLM requests per image (LENZCONTEXT_RETRIES; default: 5; 0 retries indefinitely)")
    parser.add_argument("-j", "--jobs", type=int, metavar="N",
                        help="maximum concurrent LLM analyses (LENZCONTEXT_JOBS; default: 1)")
    args = parser.parse_args(argv)
    destination = args.append or args.output or Path("lenzcontext.yaml")
    fmt = "%(levelname)s: %(name)s: %(message)s" if args.verbose else "%(levelname)s: %(message)s"
    logging.basicConfig(level=logging.INFO, format=fmt)
    # Pillow dumps raw EXIF tags at DEBUG; keep that out of CLI output.
    logging.getLogger("PIL").setLevel(logging.WARNING)
    if args.verbose:
        logging.getLogger("lenzcontext").setLevel(logging.DEBUG)
    else:
        logging.getLogger("lenzcontext").setLevel(logging.INFO)
    try:
        settings = Settings.from_env(
            structured_output=args.structured_output, timeout=args.timeout, retries=args.retries,
            geonames_db=args.geonames_db, prompt_config=args.prompt_config,
            description_language=args.description_language,
            jobs=args.jobs,
        )
    except ConfigError as exc:
        LOG.error("configuration error: %s", exc)
        return 2
    LOG.debug("inputs=%d geonames_db=%s prompt_config=%s output=%s structured_output=%s "
              "timeout=%s retries=%s description_language=%s jobs=%s",
              len(args.images), settings.geonames_db, settings.prompt_config or "<auto>",
              destination, settings.structured_output, settings.timeout, settings.retries,
              settings.description_language, settings.jobs)
    if any(destination.resolve() == Path(image).resolve() for image in args.images):
        LOG.error("output must not overwrite an input image")
        return 2
    if destination.resolve() == settings.geonames_db.resolve():
        LOG.error("output must not overwrite the GeoNames database")
        return 2
    try:
        prompts = load_prompts(settings.prompt_config)
    except ConfigError as exc:
        LOG.error("configuration error: %s", exc)
        return 2
    try:
        writer = IncrementalYamlWriter(destination, append=args.append is not None)
    except InvalidOutput as exc:
        LOG.error("invalid append target: %s", exc)
        return 2
    except OSError:
        LOG.error("could not inspect output: %s", destination)
        return 2
    database = None
    geocoder = None
    stop_event = Event()
    try:
        paths = recursive_jpegs(args.images) if args.recursive else args.images
        LOG.debug("JPEG candidates=%d", len(paths))
        try:
            database = GeoNamesDatabase(settings.geonames_db)
            geocoder = ReverseGeocoder(database)
        except (OSError, sqlite3.Error):
            LOG.warning("GeoNames database unavailable; continuing without addresses (run the importer)")
        pipeline = Pipeline(OpenAICompatibleAnalyzer(
            settings, prompts, description_language=settings.description_language,
            stop_event=stop_event), geocoder)
        LOG.info("LLM concurrency: %d", settings.jobs)
        try:
            batch = process_batch(paths, pipeline, on_success=writer.write,
                                  jobs=settings.jobs, stop_event=stop_event)
        except OSError:
            stop_event.set()
            LOG.error("could not write output: %s", destination)
            return 1
        if not batch.images:
            LOG.error("no JPEG images were successfully processed; output was not written")
            return 1
        LOG.info("wrote %d image records to %s", len(batch.images), destination)
        return 0
    except (KeyboardInterrupt, AnalysisCancelled):
        stop_event.set()
        LOG.warning("processing interrupted; records already written remain in the output")
        return 130
    finally:
        if database is not None:
            database.close()
