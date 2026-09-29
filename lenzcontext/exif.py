"""Validate JPEG bytes and extract only GPS and capture time."""

import io
import logging
import math
import re
from collections.abc import Mapping, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

from PIL import Image

from .models import ExifInfo

LOG = logging.getLogger(__name__)


class InvalidJPEG(ValueError):
    pass


def is_jpeg_path(path: Path) -> bool:
    return path.suffix.lower() in {".jpg", ".jpeg"}


def _text(value: Any) -> str:
    if isinstance(value, bytes):
        value = value.decode("ascii", errors="replace")
    return str(value).strip("\x00 ")


def dms_to_decimal(values: Sequence[Any], reference: str | bytes) -> float:
    def number(value: Any) -> float:
        if isinstance(value, (tuple, list)) and len(value) == 2:
            return float(value[0]) / float(value[1])
        return float(value)

    if len(values) != 3:
        raise ValueError("GPS requires three DMS components")
    degrees, minutes, seconds = map(number, values)
    ref = _text(reference).upper()
    if ref not in {"N", "S", "E", "W"}:
        raise ValueError("Invalid GPS direction")
    if not all(math.isfinite(v) for v in (degrees, minutes, seconds)):
        raise ValueError("Invalid GPS number")
    if degrees < 0 or not 0 <= minutes < 60 or not 0 <= seconds < 60:
        raise ValueError("Invalid GPS DMS")
    value = degrees + minutes / 60 + seconds / 3600
    if value > (90 if ref in {"N", "S"} else 180):
        raise ValueError("GPS out of range")
    return -value if ref in {"S", "W"} else value


def capture_time(tags: Mapping[int, Any]) -> str | None:
    for tag in (36867, 36868, 306):
        if not tags.get(tag):
            continue
        try:
            value = datetime.strptime(_text(tags[tag]), "%Y:%m:%d %H:%M:%S")
        except (ValueError, TypeError):
            continue
        result = value.isoformat()
        offset = _text(tags.get(36881, ""))
        if re.fullmatch(r"[+-](?:[01]\d|2[0-3]):[0-5]\d", offset):
            result += offset
        LOG.debug("capture time parsed from EXIF tag %s", tag)
        return result
    return None


def parse_exif(tags: Mapping[int, Any], gps: Mapping[int, Any]) -> ExifInfo:
    latitude = longitude = None
    try:
        if _text(gps.get(1, "")).upper() not in {"N", "S"}:
            raise ValueError("Invalid latitude reference")
        if _text(gps.get(3, "")).upper() not in {"E", "W"}:
            raise ValueError("Invalid longitude reference")
        latitude = dms_to_decimal(gps[2], gps[1])
        longitude = dms_to_decimal(gps[4], gps[3])
    except (KeyError, ValueError, TypeError, ZeroDivisionError, OverflowError):
        latitude = longitude = None
    return ExifInfo(taken_at=capture_time(tags), latitude=latitude, longitude=longitude)


def read_jpeg(path: Path) -> tuple[bytes, ExifInfo]:
    """Decoding validates the image; the original, unmodified bytes are returned."""
    if not is_jpeg_path(path):
        raise InvalidJPEG("not a JPEG extension")
    data = path.read_bytes()
    try:
        with Image.open(io.BytesIO(data)) as image:
            if image.format != "JPEG":
                raise InvalidJPEG("file content is not JPEG")
            image.load()
            tags: dict[int, Any] = {}
            gps: Mapping[int, Any] = {}
            try:
                exif = image.getexif()
                tags.update(exif)
                tags.update(exif.get_ifd(34665))  # EXIF sub-IFD
                gps = exif.get_ifd(34853)
            except (ValueError, TypeError, KeyError, IndexError, OSError, SyntaxError):
                LOG.warning("malformed EXIF in %s; using readable metadata", path)
            result = parse_exif(tags, gps)
            LOG.debug("%s: taken_at=%s latitude=%s longitude=%s",
                      path, result.taken_at or "none", result.latitude, result.longitude)
            return data, result
    except (OSError, SyntaxError, ValueError, Image.DecompressionBombError) as exc:
        raise InvalidJPEG("cannot decode JPEG") from exc
