"""Name and country-language selection, separate from location search."""

import sqlite3

from .database import GeoNamesDatabase


def primary_language(languages: str) -> str:
    return languages.split(",", 1)[0].strip()


def localized_name(database: GeoNamesDatabase, record: sqlite3.Row | dict, language: str) -> str:
    # GeoNames commonly uses base language tags where countryInfo has e.g. ko-KR.
    for tag in dict.fromkeys((language, language.split("-", 1)[0])):
        if tag and record["geoname_id"] is not None:
            alternate = database.alternate(record["geoname_id"], tag)
            if alternate:
                return alternate
    return record["name"] or record["ascii_name"]
