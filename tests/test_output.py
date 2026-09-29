import yaml

from lenzcontext.models import Address, BatchResult, ExifInfo, FileInfo, ImageResult
import pytest

from lenzcontext.output import IncrementalYamlWriter, InvalidOutput, write_yaml


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


def test_incremental_writer_appends_complete_documents(tmp_path, analysis):
    path = tmp_path / "result.yaml"
    image = ImageResult(file=FileInfo(name="one.jpg"), exif=ExifInfo(), address=None, analysis=analysis)
    writer = IncrementalYamlWriter(path)
    assert not path.exists()
    writer.write(image)
    first = path.read_text()
    assert [item["file"]["name"] for item in yaml.safe_load(first)["images"]] == ["one.jpg"]
    writer.write(image.model_copy(update={"file": FileInfo(name="two.jpg")}))
    second = path.read_text()
    assert second.startswith(first)
    assert [item["file"]["name"] for item in yaml.safe_load(second)["images"]] == ["one.jpg", "two.jpg"]
    IncrementalYamlWriter(path, append=True).write(image)
    assert [item["file"]["name"] for item in yaml.safe_load(path.read_text())["images"]] == [
        "one.jpg", "two.jpg", "one.jpg",
    ]


def test_append_accepts_legacy_and_empty_outputs(tmp_path, analysis):
    image = ImageResult(file=FileInfo(name="one.jpg"), exif=ExifInfo(), address=None, analysis=analysis)
    existing = tmp_path / "legacy.yaml"
    write_yaml(BatchResult(images=[image]), existing)
    IncrementalYamlWriter(existing, append=True).write(image)
    assert len(yaml.safe_load(existing.read_text())["images"]) == 2
    empty = tmp_path / "empty.yaml"
    empty.write_text("")
    IncrementalYamlWriter(empty, append=True).write(image)
    assert len(yaml.safe_load(empty.read_text())["images"]) == 1


@pytest.mark.parametrize("contents", [
    "not YAML: [", "version: 2\nimages: []\n", "version: 1\nimages: null\n",
    "version: 1\nimages: []\nother: value\n", "version: 1\nimages: []\n---\nversion: 1\n",
    "version: 1\nimages: []\n# trailing comment\n",
])
def test_append_rejects_noncanonical_output_without_changes(tmp_path, contents):
    path = tmp_path / "result.yaml"
    path.write_text(contents)
    with pytest.raises(InvalidOutput):
        IncrementalYamlWriter(path, append=True)
    assert path.read_text() == contents
