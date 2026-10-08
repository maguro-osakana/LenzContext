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
])
def test_append_rejects_noncanonical_output_without_changes(tmp_path, contents):
    path = tmp_path / "result.yaml"
    path.write_text(contents)
    with pytest.raises(InvalidOutput):
        IncrementalYamlWriter(path, append=True)
    assert path.read_text() == contents


@pytest.mark.parametrize('resume', [False, True])
def test_normalize_external_yaml_then_append(tmp_path, analysis, resume):
    path = tmp_path / 'result.yaml'
    image = ImageResult(file=FileInfo(name='one.jpg'), exif=ExifInfo(), address=None, analysis=analysis)
    # Reordered keys, indented list, quoted values, explicit document boundaries and comments.
    path.write_text('---\n# external tool\n' + yaml.safe_dump(
        {'images': [image.model_dump(mode='json')], 'version': 1},
        allow_unicode=True, sort_keys=True, default_flow_style=False,
    ) + '...\n')
    writer = IncrementalYamlWriter(path, append=not resume,
                                   resume_base_dir=str(tmp_path) if resume else None)
    assert path.read_text().startswith('version: 1\nimages:\n- file:')
    writer.write(image.model_copy(update={'file': FileInfo(name='two.jpg')}))
    batch = BatchResult.model_validate(yaml.safe_load(path.read_text()))
    assert [r.file.name for r in batch.images] == ['one.jpg', 'two.jpg']
    assert writer.existing_names == ({str(tmp_path / 'one.jpg')} if resume else set())
    assert not list(tmp_path.glob('.*.tmp'))


@pytest.mark.parametrize('failure', ['late_validation', 'interrupt', 'write', 'replace'])
def test_normalization_failure_preserves_original_and_removes_temp(tmp_path, analysis, monkeypatch, failure):
    import lenzcontext.output as output

    path = tmp_path / 'result.yaml'
    image = ImageResult(file=FileInfo(name='one.jpg'), exif=ExifInfo(), address=None, analysis=analysis)
    write_yaml(BatchResult(images=[image, image]), path)
    if failure == 'late_validation':
        path.write_text(path.read_text() + 'unknown: true\n')
        expected = InvalidOutput
    elif failure == 'interrupt':
        original = output._existing_images

        def interrupted(destination):
            for row in original(destination):
                yield row
                raise KeyboardInterrupt

        monkeypatch.setattr(output, '_existing_images', interrupted)
        expected = KeyboardInterrupt
    elif failure == 'write':
        def broken_dump(value):
            raise OSError('disk full')
        monkeypatch.setattr(output, '_dump', broken_dump)
        expected = OSError
    else:
        def broken_replace(*args):
            raise OSError('replace failed')
        monkeypatch.setattr(output.os, 'replace', broken_replace)
        expected = OSError
    original_bytes = path.read_bytes()
    with pytest.raises(expected):
        IncrementalYamlWriter(path, append=True)
    assert path.read_bytes() == original_bytes
    assert not list(tmp_path.glob('.*.tmp'))


def test_empty_sequence_can_receive_append(tmp_path, analysis):
    path = tmp_path / 'result.yaml'
    path.write_text('images: []\nversion: 1\n# comment\n')
    writer = IncrementalYamlWriter(path, append=True)
    assert path.read_text() == 'version: 1\nimages: []\n'
    writer.write(ImageResult(file=FileInfo(name='one.jpg'), exif=ExifInfo(), address=None, analysis=analysis))
    assert len(yaml.safe_load(path.read_text())['images']) == 1
