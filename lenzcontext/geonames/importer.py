"""Stream GeoNames dumps into an atomically published SQLite database."""

import logging
import os
import sqlite3
import sys
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from io import TextIOWrapper
from pathlib import Path
from typing import TextIO
from zipfile import BadZipFile, ZipFile

from ..console import ArgumentParser

LOG = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE geonames (
    geoname_id INTEGER PRIMARY KEY, name TEXT NOT NULL, ascii_name TEXT NOT NULL,
    latitude REAL NOT NULL, longitude REAL NOT NULL, feature_class TEXT NOT NULL,
    feature_code TEXT NOT NULL, country_code TEXT NOT NULL, admin1_code TEXT NOT NULL,
    admin2_code TEXT NOT NULL, admin3_code TEXT NOT NULL, admin4_code TEXT NOT NULL,
    population INTEGER NOT NULL
);
CREATE TABLE alternate_names (
    geoname_id INTEGER NOT NULL, language TEXT NOT NULL, name TEXT NOT NULL,
    preferred INTEGER NOT NULL
);
CREATE TABLE countries (
    country_code TEXT PRIMARY KEY, name TEXT NOT NULL, languages TEXT NOT NULL,
    geoname_id INTEGER
);
CREATE TABLE admin_codes (
    code TEXT PRIMARY KEY, name TEXT NOT NULL, ascii_name TEXT NOT NULL,
    geoname_id INTEGER NOT NULL
);
CREATE VIRTUAL TABLE places_rtree USING rtree(
    geoname_id, min_lat, max_lat, min_lon, max_lon
);
PRAGMA user_version = 1;
"""


@contextmanager
def open_dump(directory: Path, name: str) -> Iterator[TextIO]:
    """Prefer extracted files; otherwise stream the exact member from its ZIP."""
    plain = directory / name
    if plain.is_file():
        with plain.open(encoding="utf-8-sig") as stream:
            yield stream
    else:
        with ZipFile(directory / Path(name).with_suffix(".zip")) as archive:
            with archive.open(name) as raw, TextIOWrapper(raw, encoding="utf-8-sig") as stream:
                yield stream


def rows(directory: Path, name: str, minimum: int) -> Iterator[list[str]]:
    with open_dump(directory, name) as stream:
        for number, line in enumerate(stream, 1):
            if not line.strip() or line.startswith("#"):
                continue
            fields = line.rstrip("\r\n").split("\t")
            if len(fields) < minimum:
                raise ValueError(f"{name}:{number}: expected at least {minimum} columns")
            yield fields


def _insert(connection: sqlite3.Connection, sql: str, records: Iterator[tuple]) -> int:
    count = 0
    batch = []
    for record in records:
        batch.append(record)
        if len(batch) == 10_000:
            connection.executemany(sql, batch)
            count += len(batch)
            batch.clear()
            if count % 1_000_000 == 0:
                LOG.info("imported %s rows", f"{count:,}")
    connection.executemany(sql, batch)
    return count + len(batch)


def import_database(source: Path, destination: Path, *, overwrite: bool = False) -> None:
    source, destination = Path(source), Path(destination)
    if destination.exists() and not overwrite:
        raise FileExistsError("database exists; use --overwrite to replace it")
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=destination.name + ".", suffix=".tmp", dir=destination.parent)
    os.close(fd)
    connection = None
    try:
        connection = sqlite3.connect(temporary)
        # Only a disposable staging DB is modified until import succeeds.
        connection.execute("PRAGMA journal_mode=OFF")
        connection.execute("PRAGMA synchronous=OFF")
        connection.execute("PRAGMA cache_size=-65536")
        connection.executescript(SCHEMA)
        LOG.info("importing allCountries")
        count = _insert(connection, "INSERT INTO geonames VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", (
            (int(r[0]), r[1], r[2], float(r[4]), float(r[5]), r[6], r[7], r[8],
             r[10], r[11], r[12], r[13], int(r[14] or 0))
            for r in rows(source, "allCountries.txt", 19)
        ))
        if not count:
            raise ValueError("allCountries contains no records")
        LOG.info("importing alternateNamesV2")
        _insert(connection, "INSERT INTO alternate_names VALUES (?,?,?,?)", (
            (int(r[1]), r[2], r[3], int(r[4] == "1"))
            for r in rows(source, "alternateNamesV2.txt", 8)
            if r[2] not in {"link", "post", "iata", "icao", "faac", "wkdt"}
            and r[6] != "1" and r[7] != "1"  # Exclude colloquial and historical names.
        ))
        for name in ("admin1CodesASCII.txt", "admin2Codes.txt"):
            _insert(connection, "INSERT INTO admin_codes VALUES (?,?,?,?)", (
                (r[0], r[1], r[2], int(r[3])) for r in rows(source, name, 4)
            ))
        _insert(connection, "INSERT INTO countries VALUES (?,?,?,?)", (
            (r[0], r[4], r[15], int(r[16]) if r[16] else None)
            for r in rows(source, "countryInfo.txt", 17)
        ))
        LOG.info("building spatial and name indexes")
        connection.executescript("""
            INSERT INTO places_rtree
            SELECT geoname_id, latitude, latitude, longitude, longitude
            FROM geonames WHERE feature_class = 'P';
            CREATE INDEX alternate_lookup ON alternate_names(geoname_id, language, preferred DESC);
            CREATE INDEX admin_lookup ON geonames(
                country_code, feature_code, admin1_code, admin2_code, admin3_code, admin4_code
            ) WHERE feature_class = 'A';
            ANALYZE;
        """)
        connection.commit()
        connection.close()
        connection = None
        if overwrite:
            os.replace(temporary, destination)
        else:
            # link refuses a destination created by another process during import.
            os.link(temporary, destination)
        LOG.info("wrote GeoNames database to %s", destination)
    finally:
        if connection is not None:
            connection.close()
        Path(temporary).unlink(missing_ok=True)


def main(argv: list[str] | None = None) -> int:
    parser = ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path, help="directory containing GeoNames dumps or ZIPs")
    parser.add_argument("-o", "--output", type=Path, default=Path("data/geonames.db"))
    parser.add_argument("--overwrite", action="store_true")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s", stream=sys.stdout)
    try:
        import_database(args.source, args.output, overwrite=args.overwrite)
    except (OSError, ValueError, sqlite3.Error, KeyError, BadZipFile) as exc:
        LOG.error("GeoNames import failed: %s", exc)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
