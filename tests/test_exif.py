import pytest
from PIL import Image

from lenzcontext.exif import InvalidJPEG, capture_time, dms_to_decimal, is_jpeg_path, parse_exif, read_jpeg


@pytest.mark.parametrize("extension", ["jpg", "jpeg", "JPG", "JPEG"])
def test_jpeg_extensions_and_original_bytes(make_jpeg, extension):
    path = make_jpeg("photo." + extension)
    assert is_jpeg_path(path)
    data, exif = read_jpeg(path)
    assert data == path.read_bytes()
    assert exif.model_dump() == {"taken_at": None, "latitude": None, "longitude": None}


def test_fake_jpeg_and_truncated_jpeg(tmp_path, make_jpeg):
    path = tmp_path / "fake.jpg"
    Image.new("RGB", (8, 8)).save(path, format="PNG")
    with pytest.raises(InvalidJPEG):
        read_jpeg(path)
    path = make_jpeg()
    path.write_bytes(path.read_bytes()[:100])
    with pytest.raises(InvalidJPEG):
        read_jpeg(path)


@pytest.mark.parametrize("ref,expected", [("N", 37.5665), ("S", -37.5665), ("E", 37.5665), (b"W", -37.5665)])
def test_dms(ref, expected):
    assert dms_to_decimal(((37, 1), (33, 1), (594, 10)), ref) == pytest.approx(expected)


@pytest.mark.parametrize("tags,expected", [
    ({36867: "2026:04:12 14:23:11", 36868: "2020:01:01 00:00:00"}, "2026-04-12T14:23:11"),
    ({36868: "2026:04:12 14:23:11"}, "2026-04-12T14:23:11"),
    ({306: "2026:04:12 14:23:11"}, "2026-04-12T14:23:11"),
    ({36867: "2026:04:12 14:23:11", 36881: "+09:00"}, "2026-04-12T14:23:11+09:00"),
    ({36867: "2026:04:12 14:23:11", 36881: b"-03:30"}, "2026-04-12T14:23:11-03:30"),
    ({36867: "invalid", 36868: "2026:04:12 14:23:11"}, "2026-04-12T14:23:11"),
    ({36867: "2026:04:12 14:23:11", 36881: "+99:99"}, "2026-04-12T14:23:11"),
    ({}, None),
])
def test_time_priority(tags, expected):
    assert capture_time(tags) == expected


def test_real_exif_sub_ifds(make_jpeg):
    exif = Image.Exif()
    exif[306] = "2020:01:01 00:00:00"
    exif[34665] = {36867: "2026:04:12 14:23:11", 36881: "+09:00"}
    exif[34853] = {1: "S", 2: (37.0, 30.0, 0.0), 3: "W", 4: (126.0, 45.0, 0.0)}
    path = make_jpeg(exif=exif)
    data, info = read_jpeg(path)
    assert data == path.read_bytes()
    assert info.taken_at == "2026-04-12T14:23:11+09:00"
    assert info.latitude == -37.5
    assert info.longitude == -126.75


def test_invalid_or_incomplete_gps_is_absent():
    for gps in ({}, {1: "N", 2: (1, 2, 3)}, {1: "N", 2: (999, 0, 0), 3: "E", 4: (1, 0, 0)}):
        info = parse_exif({}, gps)
        assert info.latitude is info.longitude is None
