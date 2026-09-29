import yaml

from lenzcontext.models import Address, BatchResult, ExifInfo, FileInfo, ImageResult
from lenzcontext.output import write_yaml


def test_unicode_normalized_ocr_and_single_yaml(tmp_path, analysis):
    images = [ImageResult(file=FileInfo(name=name), exif=ExifInfo(taken_at="2026-04-12T14:23:11+09:00"),
                          address=Address(english="Seoul", local="대한민국 서울특별시 중구"), analysis=analysis)
              for name in ("C.jpg", "A.jpg")]
    path = tmp_path / "result.yaml"
    write_yaml(BatchResult(images=images), path)
    text = path.read_text()
    documents = list(yaml.safe_load_all(text))
    assert len(documents) == 1
    assert documents[0]["version"] == 1
    assert [r["file"]["name"] for r in documents[0]["images"]] == ["C.jpg", "A.jpg"]
    assert documents[0]["images"][0]["exif"]["taken_at"] == "2026-04-12T14:23:11+09:00"
    assert documents[0]["images"][0]["analysis"]["ocr"]["text"] == "서울역 Welcome 出口"
    assert "대한민국" in text
    assert "\\u" not in text
