"""Distance-first populated-place lookup and administrative address assembly."""

import math
import sqlite3

from ..models import Address
from .database import EARTH_RADIUS_KM, GeoNamesDatabase
from .language import localized_name, primary_language

FEATURE_RANK = {"PPLC": 0, "PPLA": 1, "PPLA2": 2, "PPLA3": 3, "PPLA4": 4, "PPL": 5, "PPLX": 6}


def haversine(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    a, b = math.radians(lat1), math.radians(lat2)
    dlat, dlon = b - a, math.radians(lon2 - lon1)
    value = math.sin(dlat / 2) ** 2 + math.cos(a) * math.cos(b) * math.sin(dlon / 2) ** 2
    return 2 * EARTH_RADIUS_KM * math.asin(math.sqrt(min(1.0, max(0.0, value))))


def _unique(names: list[str]) -> list[str]:
    seen = set()
    result = []
    for name in names:
        key = name.strip().casefold()
        if key and key not in seen:
            seen.add(key)
            result.append(name)
    return result


class ReverseGeocoder:
    def __init__(self, database: GeoNamesDatabase, max_distance_km: float = 100):
        if not math.isfinite(max_distance_km) or not 0 < max_distance_km <= 20_000:
            raise ValueError("max distance must be positive and at most 20000 km")
        self.database = database
        self.max_distance_km = max_distance_km

    def reverse(self, latitude: float, longitude: float) -> Address | None:
        if not (math.isfinite(latitude) and math.isfinite(longitude)
                and -90 <= latitude <= 90 and -180 <= longitude <= 180):
            raise ValueError("invalid coordinates")
        radii = sorted({min(r, self.max_distance_km) for r in (1, 5, 25, self.max_distance_km)})
        for radius in radii:
            candidates = self.database.candidates(latitude, longitude, radius)
            ranked = sorted(
                ((haversine(latitude, longitude, p["latitude"], p["longitude"]), p) for p in candidates),
                key=lambda pair: (pair[0], FEATURE_RANK.get(pair[1]["feature_code"], 99),
                                  -pair[1]["population"], pair[1]["geoname_id"]),
            )
            if ranked and ranked[0][0] <= radius:
                return self._address(ranked[0][1])
        return None

    def _address(self, place: sqlite3.Row) -> Address:
        country = self.database.country(place["country_code"])
        language = primary_language(country["languages"]) if country else ""
        records = [place]
        for level in (4, 3, 2, 1):
            admin = self.database.administrative(place, level)
            if admin:
                records.append(admin)
        if country:
            records.append(self.database.get(country["geoname_id"]) or {
                "geoname_id": country["geoname_id"], "name": country["name"], "ascii_name": country["name"],
            })
        # IDs also deduplicate records whose labels differ across language fallbacks.
        records = list({r["geoname_id"]: r for r in records}.values())
        english = _unique([localized_name(self.database, r, "en") for r in records])
        local = _unique([localized_name(self.database, r, language) for r in records])
        # Broad-to-narrow matches conventional East Asian local address ordering.
        if language.split("-", 1)[0] in {"ko", "ja", "zh"}:
            local.reverse()
            local_text = " ".join(local)
        else:
            local_text = ", ".join(local)
        return Address(english=", ".join(english), local=local_text)
