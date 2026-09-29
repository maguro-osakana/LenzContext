from pathlib import Path

import pytest
from PIL import Image

from lenzcontext.geonames.importer import import_database
from lenzcontext.models import AnalysisResult, OCRResult


@pytest.fixture
def analysis():
    return AnalysisResult(description_en="A street with shops and pedestrians.",
                          description="A street with shops and pedestrians.",
                          ocr=OCRResult(detected=True, text="서울역\nWelcome\n出口"),
                          screenshot_probability=0.02)


@pytest.fixture
def make_jpeg(tmp_path):
    def make(name="image.jpg", exif=None):
        path = tmp_path / name
        kwargs = {"exif": exif} if exif is not None else {}
        Image.new("RGB", (12, 8), (100, 50, 0)).save(path, format="JPEG", **kwargs)
        return path
    return make


def place_row(gid, name, lat, lon, cls="P", feature="PPL", country="KR",
              admin1="11", admin2="140", admin3="", admin4="", population=0):
    return [str(gid), name, name, "", str(lat), str(lon), cls, feature, country,
            "", admin1, admin2, admin3, admin4, str(population), "", "", "Asia/Seoul", "2026-01-01"]


@pytest.fixture
def geonames_source(tmp_path):
    source = tmp_path / "dumps"
    source.mkdir()
    places = [
        place_row(1, "Jung-gu", 37.5665, 126.978),
        place_row(2, "Huge City", 37.58, 126.99, population=10_000_000, feature="PPLC"),
        place_row(3, "Seoul", 37.55, 126.98, cls="A", feature="ADM1", admin2=""),
        place_row(4, "Jung-gu", 37.566, 126.978, cls="A", feature="ADM2"),
        place_row(5, "Republic of Korea", 36, 128, cls="A", feature="PCLI", admin1="", admin2=""),
        place_row(6, "Dateline Village", 0, -179.999, country="", admin1="", admin2=""),
        place_row(7, "Polar Village", 89.999, 140, country="", admin1="", admin2=""),
        place_row(8, "Nonpopulated landmark", 37.5665, 126.978, cls="S", feature="BLDG"),
    ]
    (source / "allCountries.txt").write_text("\n".join("\t".join(r) for r in places) + "\n", encoding="utf-8")
    alternates = [
        (1, 1, "en", "Central District", "", "", "", ""),
        (2, 1, "en", "Jung-gu", "1", "", "", ""),
        (3, 1, "ko", "중구", "1", "", "", ""),
        (4, 3, "en", "Seoul", "1", "", "", ""),
        (5, 3, "ko", "서울특별시", "1", "", "", ""),
        (6, 4, "en", "Jung-gu", "1", "", "", ""),
        (7, 4, "ko", "중구", "1", "", "", ""),
        (8, 5, "en", "South Korea", "1", "", "", ""),
        (9, 5, "ko", "대한민국", "1", "", "", ""),
        (10, 1, "en", "Ancient district", "1", "", "", "1"),
    ]
    (source / "alternateNamesV2.txt").write_text(
        "\n".join("\t".join(map(str, r)) for r in alternates) + "\n", encoding="utf-8")
    (source / "admin1CodesASCII.txt").write_text("KR.11\tSeoul\tSeoul\t3\n", encoding="utf-8")
    (source / "admin2Codes.txt").write_text("KR.11.140\tJung-gu\tJung-gu\t4\n", encoding="utf-8")
    country = ["KR", "KOR", "410", "KS", "South Korea", "Seoul", "0", "0", "AS", "kr", "KRW", "Won", "82", "", "", "ko-KR,en", "5", "", ""]
    (source / "countryInfo.txt").write_text("# comment\n" + "\t".join(country) + "\n", encoding="utf-8")
    return source


@pytest.fixture
def geonames_db(geonames_source, tmp_path):
    path = tmp_path / "geonames.db"
    import_database(geonames_source, path)
    return path
