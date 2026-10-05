import pytest
import yaml

from lenzcontext.cli import main, recursive_jpegs


def test_recursive_jpegs_multiple_roots_and_symlinks(tmp_path):
    root = tmp_path / "photos"
    nested = root / "nested" / "deep"
    nested.mkdir(parents=True)
    other = tmp_path / "other"
    other.mkdir()
    external = tmp_path / "external"
    external.mkdir()
    for path in (root / "one.jpg", nested / "two.JPEG", other / "three.JPG",
                 external / "four.jpeg", root / "ignore.png", nested / "ignore.txt"):
        path.touch()
    (root / "linked-directory").symlink_to(external, target_is_directory=True)
    (root / "linked-file.JPEG").symlink_to(external / "four.jpeg")
    assert set(recursive_jpegs([str(root), str(other)])) == {
        str(root / "one.jpg"), str(nested / "two.JPEG"), str(other / "three.JPG"),
        str(root / "linked-directory" / "four.jpeg"), str(root / "linked-file.JPEG"),
    }


@pytest.mark.parametrize("flag", ["-R", "--recursive"])
@pytest.mark.parametrize("jobs", [1, 2])
def test_cli_recursive_processing(flag, jobs, monkeypatch, make_jpeg, tmp_path, analysis):
    root = tmp_path / "photos"
    (root / "nested").mkdir(parents=True)
    images = [make_jpeg("photos/one.jpg"), make_jpeg("photos/nested/two.JPEG")]
    (root / "bad.jpg").write_bytes(b"not a JPEG")
    (root / "ignore.png").write_bytes(images[0].read_bytes())
    monkeypatch.setenv("LENZCONTEXT_MODEL", "mock-vision")
    monkeypatch.setattr("lenzcontext.llm.openai_compatible.OpenAICompatibleAnalyzer.analyze",
                        lambda *args: analysis)
    output = tmp_path / "out.yaml"
    assert main([flag, str(root), "--jobs", str(jobs), "-o", str(output),
                 "--geonames-db", str(tmp_path / "missing.db")]) == 0
    records = yaml.safe_load(output.read_text())["images"]
    assert len(records) == 2
    assert {record["file"]["name"] for record in records} == {str(path) for path in images}


@pytest.mark.parametrize("existing", [False, True])
def test_cli_recursive_empty_directory_preserves_output(existing, monkeypatch, tmp_path):
    root = tmp_path / "empty"
    root.mkdir()
    output = tmp_path / "out.yaml"
    if existing:
        output.write_text("existing output\n")
    monkeypatch.setenv("LENZCONTEXT_MODEL", "mock-vision")
    assert main(["-R", str(root), "-o", str(output),
                 "--geonames-db", str(tmp_path / "missing.db")]) == 1
    if existing:
        assert output.read_text() == "existing output\n"
    else:
        assert not output.exists()
