import sqlite3
from zipfile import ZipFile

import pytest

from lenzcontext.geonames.database import GeoNamesDatabase
from lenzcontext.geonames.importer import import_database
from lenzcontext.geonames.language import localized_name, primary_language
from lenzcontext.geonames.reverse import ReverseGeocoder, haversine


def test_nearest_and_admin_localized_address(geonames_db):
    with GeoNamesDatabase(geonames_db) as db:
        address = ReverseGeocoder(db).reverse(37.5665, 126.978)
        assert address.english == "Jung-gu, Seoul, South Korea"
        assert address.local == "대한민국 서울특별시 중구"
        # Bigger city farther away and exact-coordinate non-P records do not win.
        assert "Huge City" not in address.english
        assert db.administrative(db.get(1), 2)["geoname_id"] == 4
        assert localized_name(db, db.get(1), "fr") == "Jung-gu"
        assert db.alternate(1, "en") == "Jung-gu"


def test_language_selection():
    assert primary_language("ko-KR,en") == "ko-KR"
    assert primary_language("de,fr,it") == "de"
    assert primary_language("") == ""


def test_dateline_poles_and_no_candidate(geonames_db):
    with GeoNamesDatabase(geonames_db) as db:
        reverse = ReverseGeocoder(db)
        assert reverse.reverse(0, 179.999).english == "Dateline Village"
        assert reverse.reverse(90, -40).english == "Polar Village"
        assert reverse.reverse(-30, 0) is None
        with pytest.raises(ValueError):
            reverse.reverse(float("nan"), 0)
    assert haversine(0, 179.999, 0, -179.999) < 0.23


def test_stream_zips(geonames_source, tmp_path):
    for filename in ("allCountries.txt", "alternateNamesV2.txt"):
        path = geonames_source / filename
        with ZipFile(path.with_suffix(".zip"), "w") as archive:
            archive.write(path, filename)
        path.unlink()
    destination = tmp_path / "zipped.db"
    import_database(geonames_source, destination)
    with GeoNamesDatabase(destination) as db:
        assert db.get(1)["name"] == "Jung-gu"


def test_import_preserves_previous_database_on_failure(geonames_source, geonames_db):
    before = geonames_db.read_bytes()
    with pytest.raises(FileExistsError):
        import_database(geonames_source, geonames_db)
    (geonames_source / "alternateNamesV2.txt").write_text("bad\n")
    with pytest.raises(ValueError):
        import_database(geonames_source, geonames_db, overwrite=True)
    assert geonames_db.read_bytes() == before
    assert not list(geonames_db.parent.glob("*.tmp"))


def test_missing_database_does_not_create_file(tmp_path):
    path = tmp_path / "absent.db"
    with pytest.raises(sqlite3.OperationalError):
        GeoNamesDatabase(path)
    assert not path.exists()


def test_historical_places_are_not_current_addresses(geonames_db):
    with sqlite3.connect(geonames_db) as connection:
        connection.execute("UPDATE geonames SET feature_code='PPLH' WHERE geoname_id=1")
    with GeoNamesDatabase(geonames_db) as db:
        address = ReverseGeocoder(db).reverse(37.5665, 126.978)
        assert address.english.startswith("Huge City,")


def test_admin3_and_admin4_resolution(geonames_db):
    with sqlite3.connect(geonames_db) as connection:
        connection.execute("UPDATE geonames SET admin3_code='3', admin4_code='4' WHERE geoname_id=1")
        for gid, feature, name, admin4 in [(20, "ADM3", "Third level", ""), (21, "ADM4", "Fourth level", "4")]:
            connection.execute("INSERT INTO geonames VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                               (gid, name, name, 37.56, 126.97, "A", feature, "KR", "11", "140", "3", admin4, 0))
    with GeoNamesDatabase(geonames_db) as db:
        address = ReverseGeocoder(db).reverse(37.5665, 126.978)
        assert address.english == "Jung-gu, Fourth level, Third level, Seoul, South Korea"


def test_distance_precedes_population_and_feature_within_search_box(geonames_db):
    with sqlite3.connect(geonames_db) as connection:
        # Point 1 lies inside the 1 km bounding box but outside the 1 km circle.
        for gid, lat, lon in [(1, 0.008, 0.008), (2, 0.01, 0.0)]:
            connection.execute("UPDATE geonames SET latitude=?, longitude=? WHERE geoname_id=?", (lat, lon, gid))
            connection.execute("UPDATE places_rtree SET min_lat=?,max_lat=?,min_lon=?,max_lon=? WHERE geoname_id=?",
                               (lat, lat, lon, lon, gid))
    with GeoNamesDatabase(geonames_db) as db:
        assert ReverseGeocoder(db).reverse(0, 0).english.startswith("Huge City,")
