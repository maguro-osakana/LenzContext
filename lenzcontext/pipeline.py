"""Per-image orchestration, independent of the LLM provider."""

import logging
import sqlite3
from pathlib import Path
from typing import Protocol

from .exif import read_jpeg
from .llm.base import VisionAnalyzer
from .models import Address, FileInfo, ImageResult

LOG = logging.getLogger(__name__)


class Geocoder(Protocol):
    def reverse(self, latitude: float, longitude: float) -> Address | None: ...


class Pipeline:
    def __init__(self, analyzer: VisionAnalyzer, geocoder: Geocoder | None = None):
        self.analyzer = analyzer
        self.geocoder = geocoder

    def process(self, path: Path) -> ImageResult:
        jpeg, exif = read_jpeg(path)
        address = None
        if self.geocoder is None:
            LOG.debug("%s: geocoding disabled (no GeoNames database)", path)
        elif exif.latitude is None or exif.longitude is None:
            LOG.debug("%s: no GPS; skipping reverse geocoding", path)
        else:
            LOG.debug("%s: reverse geocoding (%.6f, %.6f)", path, exif.latitude, exif.longitude)
            try:
                address = self.geocoder.reverse(exif.latitude, exif.longitude)
            except (sqlite3.Error, OSError, ValueError):
                LOG.warning("local reverse geocoding failed for %s; continuing without address", path)
            LOG.debug("%s: address=%s", path, address.model_dump() if address else None)
        analysis = self.analyzer.analyze(jpeg, address, exif.taken_at)
        LOG.debug("%s: analysis accepted (description=%d chars, ocr_detected=%s, screenshot_probability=%s)",
                  path, len(analysis.description_en), analysis.ocr.detected, analysis.screenshot_probability)
        return ImageResult(file=FileInfo(name=path.name), exif=exif, address=address, analysis=analysis)
