"""Read-only indexed access to the local GeoNames database."""

import math
import sqlite3
from pathlib import Path

EARTH_RADIUS_KM = 6371.0088


class GeoNamesDatabase:
    def __init__(self, path: Path):
        self.connection = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True)
        self.connection.row_factory = sqlite3.Row
        try:
            if self.connection.execute("PRAGMA user_version").fetchone()[0] != 1:
                raise sqlite3.DatabaseError("unsupported GeoNames database; run the importer")
            for table in ("geonames", "countries", "admin_codes", "alternate_names", "places_rtree"):
                self.connection.execute(f"SELECT * FROM {table} LIMIT 0")
        except sqlite3.Error:
            self.connection.close()
            raise

    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> "GeoNamesDatabase":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def get(self, geoname_id: int | None) -> sqlite3.Row | None:
        return self.connection.execute("SELECT * FROM geonames WHERE geoname_id=?", (geoname_id,)).fetchone()

    def country(self, code: str) -> sqlite3.Row | None:
        return self.connection.execute("SELECT * FROM countries WHERE country_code=?", (code,)).fetchone()

    def alternate(self, geoname_id: int, language: str) -> str | None:
        row = self.connection.execute(
            "SELECT name FROM alternate_names WHERE geoname_id=? AND language=? "
            "AND name != '' ORDER BY preferred DESC, name LIMIT 1", (geoname_id, language),
        ).fetchone()
        return row[0] if row else None

    def administrative(self, place: sqlite3.Row, level: int) -> sqlite3.Row | dict | None:
        codes = [place[f"admin{i}_code"] for i in range(1, level + 1)]
        if not all(codes):
            return None
        if level <= 2:
            code = ".".join([place["country_code"], *codes])
            entry = self.connection.execute("SELECT * FROM admin_codes WHERE code=?", (code,)).fetchone()
            if entry:
                return self.get(entry["geoname_id"]) or dict(entry)
        conditions = " AND ".join(f"admin{i}_code=?" for i in range(1, level + 1))
        return self.connection.execute(
            "SELECT * FROM geonames WHERE feature_class='A' AND country_code=? "
            f"AND feature_code=? AND {conditions} ORDER BY geoname_id LIMIT 1",
            (place["country_code"], f"ADM{level}", *codes),
        ).fetchone()

    def candidates(self, latitude: float, longitude: float, radius_km: float) -> list[sqlite3.Row]:
        angle = min(radius_km / EARTH_RADIUS_KM, math.pi)
        delta_lat = math.degrees(angle)
        low_lat, high_lat = max(-90, latitude - delta_lat), min(90, latitude + delta_lat)
        if low_lat <= -90 or high_lat >= 90:
            ranges = [(-180, 180)]
        else:
            delta_lon = math.degrees(math.asin(min(1, math.sin(angle) / math.cos(math.radians(latitude)))))
            low, high = longitude - delta_lon, longitude + delta_lon
            if low < -180:
                ranges = [(-180, high), (low + 360, 180)]
            elif high > 180:
                ranges = [(low, 180), (-180, high - 360)]
            else:
                ranges = [(low, high)]
        found = {}
        for low, high in ranges:
            for row in self.connection.execute(
                "SELECT g.* FROM places_rtree r JOIN geonames g USING (geoname_id) "
                "WHERE r.max_lat>=? AND r.min_lat<=? AND r.max_lon>=? AND r.min_lon<=? "
                "AND g.feature_code NOT IN ('PPLH', 'PPLQ', 'PPLW')",
                (low_lat, high_lat, low, high),
            ):
                found[row["geoname_id"]] = row
        return list(found.values())
