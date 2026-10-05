"""Ordered batch CLI; individual failures never discard successful records."""

import logging
import os
import sqlite3
import sys
from pathlib import Path
from threading import Event

from . import __version__
from .batch import process_batch
from .console import ArgumentParser
from .config import ConfigError, Settings, load_prompts
from .exif import is_jpeg_path
from .geonames.database import GeoNamesDatabase
from .geonames.reverse import ReverseGeocoder
from .llm.base import AnalysisCancelled
from .llm.openai_compatible import OpenAICompatibleAnalyzer
from .output import IncrementalYamlWriter, InvalidOutput, path_key
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


def main(argv: list[str] | None = None) -> int:
    parser = ArgumentParser(prog="lenzcontext", description="Analyze JPEG images into one YAML file.")
    parser.add_argument("-v", "--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("-V", "--verbose", action="store_true", help="enable DEBUG diagnostics on stdout")
    parser.add_argument("images", nargs="+", help="JPEG files, or directories with -R")
    parser.add_argument("-R", "--recursive", action="store_true",
                        help="recursively find JPEG files in input directories, following symbolic links")
    output_group = parser.add_mutually_exclusive_group()
    output_group.add_argument("-o", "--output", type=Path, help="output YAML file (default: lenzcontext.yaml)")
    output_group.add_argument("-a", "--append", type=Path, metavar="FILE", help="append records to an existing YAML file")
    output_group.add_argument("--resume", type=Path, metavar="FILE",
                              help="skip images already recorded in FILE and append new results")
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
    base_dir = os.getcwd()
    destination = args.resume or args.append or args.output or Path("lenzcontext.yaml")
    fmt = "%(levelname)s: %(name)s: %(message)s" if args.verbose else "%(levelname)s: %(message)s"
    logging.basicConfig(level=logging.INFO, format=fmt, stream=sys.stdout)
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
        writer = IncrementalYamlWriter(destination, append=args.append is not None,
                                       resume_base_dir=base_dir if args.resume is not None else None)
    except InvalidOutput as exc:
        LOG.error("invalid append/resume target: %s", exc)
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
        if args.resume is not None:
            remaining = [path for path in paths if path_key(str(path), base_dir) not in writer.existing_names]
            skipped = len(paths) - len(remaining)
            LOG.info("resume: skipped %d existing image records", skipped)
            paths = remaining
            if skipped and not paths:
                return 0
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
