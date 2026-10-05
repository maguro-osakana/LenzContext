import logging
import sqlite3
from unittest.mock import Mock

import pytest
import yaml
from PIL import Image

from lenzcontext import __version__
from lenzcontext.cli import main, process_batch
from lenzcontext.llm.base import LLMError
from lenzcontext.pipeline import Pipeline


def test_order_mixed_batch_and_failure_recovery(make_jpeg, tmp_path, analysis, caplog):
    paths = [make_jpeg("C.jpg"), tmp_path / "screenshot.png", make_jpeg("A.JPG"), make_jpeg("failed.jpeg"), make_jpeg("B.JPEG")]
    analyzer = Mock()
    analyzer.analyze.side_effect = [analysis, analysis, LLMError("LLM API returned HTTP 500"), analysis]
    batch = process_batch(paths, Pipeline(analyzer))
    assert [r.file.name for r in batch.images] == [str(paths[i]) for i in (0, 2, 4)]
    assert "skipping non-JPEG file:" in caplog.text
    assert "500" in caplog.text
    assert analyzer.analyze.call_count == 4


def test_no_gps_does_not_call_geocoder(make_jpeg, analysis):
    geocoder = Mock()
    analyzer = Mock()
    analyzer.analyze.return_value = analysis
    path = make_jpeg()
    result = Pipeline(analyzer, geocoder).process(path)
    geocoder.reverse.assert_not_called()
    analyzer.analyze.assert_called_once_with(path.read_bytes(), None, None)
    assert result.address is None


def test_geocoding_failure_still_analyzes(make_jpeg, analysis):
    exif = Image.Exif()
    exif[34853] = {1: "N", 2: (37.0, 30.0, 0.0), 3: "E", 4: (126.0, 45.0, 0.0)}
    geocoder = Mock()
    geocoder.reverse.side_effect = sqlite3.OperationalError("broken DB")
    analyzer = Mock()
    analyzer.analyze.return_value = analysis
    result = Pipeline(analyzer, geocoder).process(make_jpeg(exif=exif))
    assert result.address is None
    assert result.exif.latitude == 37.5
    analyzer.analyze.assert_called_once()


def test_cli_success_and_empty_batch(monkeypatch, make_jpeg, tmp_path, analysis):
    monkeypatch.setenv("LENZCONTEXT_MODEL", "mock-vision")
    monkeypatch.setattr("lenzcontext.llm.openai_compatible.OpenAICompatibleAnalyzer.analyze", lambda *args: analysis)
    output = tmp_path / "out.yaml"
    args = [str(make_jpeg()), "-o", str(output), "--geonames-db", str(tmp_path / "missing.db")]
    assert main(args) == 0
    before = output.read_text()
    assert "description_en:" in before
    assert main([str(tmp_path / "x.png"), "-o", str(output)]) == 1
    assert output.read_text() == before


@pytest.mark.parametrize("input_path", ["photos/image.jpg", "./photos/image.jpg", "photos//image.jpg", "photos/../photos/image.jpg"])
@pytest.mark.parametrize("jobs", [1, 2])
def test_cli_preserves_input_path(monkeypatch, make_jpeg, tmp_path, analysis, input_path, jobs):
    image = make_jpeg()
    photos = tmp_path / "photos"
    photos.mkdir()
    (photos / "image.jpg").write_bytes(image.read_bytes())
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("LENZCONTEXT_MODEL", "mock-vision")
    monkeypatch.setattr("lenzcontext.llm.openai_compatible.OpenAICompatibleAnalyzer.analyze", lambda *args: analysis)
    output = tmp_path / "out.yaml"
    assert main([input_path, "-o", str(output), "--jobs", str(jobs)]) == 0
    assert yaml.safe_load(output.read_text())["images"][0]["file"]["name"] == input_path


def test_output_cannot_overwrite_input(monkeypatch, make_jpeg):
    path = make_jpeg()
    before = path.read_bytes()
    assert main([str(path), "-o", str(path)]) == 2
    assert path.read_bytes() == before


@pytest.mark.parametrize("flag", ["-v", "--version"])
def test_version_flag(flag, capsys):
    with pytest.raises(SystemExit) as raised:
        main([flag])
    assert raised.value.code == 0
    assert capsys.readouterr().out.strip() == f"lenzcontext {__version__}"


def test_verbose_flag_enables_debug_logging(monkeypatch, make_jpeg, tmp_path, analysis, caplog):
    monkeypatch.setenv("LENZCONTEXT_MODEL", "mock-vision")
    monkeypatch.setattr("lenzcontext.llm.openai_compatible.OpenAICompatibleAnalyzer.analyze", lambda *args: analysis)
    output = tmp_path / "out.yaml"
    args = [str(make_jpeg()), "-o", str(output), "--geonames-db", str(tmp_path / "missing.db"), "-V"]
    assert main(args) == 0
    assert "inputs=1" in caplog.text
    assert "geocoding disabled" in caplog.text
    assert "batch summary: 1 succeeded" in caplog.text


def test_default_logging_stays_at_info(monkeypatch, make_jpeg, tmp_path, analysis, caplog):
    caplog.set_level(logging.INFO)
    monkeypatch.setenv("LENZCONTEXT_MODEL", "mock-vision")
    monkeypatch.setattr("lenzcontext.llm.openai_compatible.OpenAICompatibleAnalyzer.analyze", lambda *args: analysis)
    output = tmp_path / "out.yaml"
    args = [str(make_jpeg()), "-o", str(output), "--geonames-db", str(tmp_path / "missing.db")]
    assert main(args) == 0
    assert "inputs=1" not in caplog.text


def test_invalid_reasoning_fails_before_analysis(monkeypatch, make_jpeg, tmp_path, caplog):
    monkeypatch.setenv("LENZCONTEXT_MODEL", "mock-vision")
    analyze = Mock()
    monkeypatch.setattr("lenzcontext.llm.openai_compatible.OpenAICompatibleAnalyzer.analyze", analyze)
    config = tmp_path / "prompts.yaml"
    config.write_text("vision:\n  system: s\n  user: u\n  reasoning:\n    enabled: true\n    token_budget: 0\n")
    output = tmp_path / "out.yaml"
    assert main([str(make_jpeg()), "--prompt-config", str(config), "-o", str(output)]) == 2
    analyze.assert_not_called()
    assert not output.exists()
    assert "vision.reasoning.token_budget" in caplog.text



def test_cli_writes_each_success_before_next_image(monkeypatch, make_jpeg, tmp_path, analysis):
    monkeypatch.setenv("LENZCONTEXT_MODEL", "mock-vision")
    paths = [make_jpeg("one.jpg"), make_jpeg("two.jpg")]
    output = tmp_path / "result.yaml"
    calls = []

    def analyze(*args):
        calls.append(1)
        if len(calls) == 2:
            assert [row["file"]["name"] for row in yaml.safe_load(output.read_text())["images"]] == [str(paths[0])]
        return analysis

    monkeypatch.setattr("lenzcontext.llm.openai_compatible.OpenAICompatibleAnalyzer.analyze", analyze)
    assert main([*(str(p) for p in paths), "-o", str(output),
                 "--geonames-db", str(tmp_path / "missing.db"), "--jobs", "1"]) == 0
    assert [row["file"]["name"] for row in yaml.safe_load(output.read_text())["images"]] == [str(p) for p in paths]


def test_cli_append_and_empty_batch(monkeypatch, make_jpeg, tmp_path, analysis):
    monkeypatch.setenv("LENZCONTEXT_MODEL", "mock-vision")
    monkeypatch.setattr("lenzcontext.llm.openai_compatible.OpenAICompatibleAnalyzer.analyze", lambda *args: analysis)
    image = make_jpeg("one.jpg")
    output = tmp_path / "result.yaml"
    assert main([str(image), "-a", str(output)]) == 0
    first = output.read_text()
    assert main([str(tmp_path / "not-jpeg.png"), "-a", str(output)]) == 1
    assert output.read_text() == first
    assert main([str(image), "-a", str(output)]) == 0
    assert output.read_text().startswith(first)
    assert len(yaml.safe_load(output.read_text())["images"]) == 2


def test_cli_append_rejects_invalid_file_before_analysis(monkeypatch, make_jpeg, tmp_path):
    monkeypatch.setenv("LENZCONTEXT_MODEL", "mock-vision")
    analyze = Mock()
    monkeypatch.setattr("lenzcontext.llm.openai_compatible.OpenAICompatibleAnalyzer.analyze", analyze)
    output = tmp_path / "result.yaml"
    output.write_text("not yaml: [")
    assert main([str(make_jpeg()), "-a", str(output)]) == 2
    assert output.read_text() == "not yaml: ["
    analyze.assert_not_called()


def test_cli_output_options_are_exclusive(make_jpeg, tmp_path):
    with pytest.raises(SystemExit) as raised:
        main([str(make_jpeg()), "-o", str(tmp_path / "out.yaml"), "-a", str(tmp_path / "append.yaml")])
    assert raised.value.code == 2


def test_cli_write_error_stops_batch_and_keeps_completed_records(monkeypatch, make_jpeg, tmp_path, analysis):
    monkeypatch.setenv("LENZCONTEXT_MODEL", "mock-vision")
    analyze = Mock(return_value=analysis)
    monkeypatch.setattr("lenzcontext.llm.openai_compatible.OpenAICompatibleAnalyzer.analyze", analyze)
    from lenzcontext.output import IncrementalYamlWriter

    original_write = IncrementalYamlWriter.write
    writes = 0

    def write_once(self, image):
        nonlocal writes
        writes += 1
        if writes == 2:
            raise OSError("disk full")
        original_write(self, image)

    monkeypatch.setattr(IncrementalYamlWriter, "write", write_once)
    paths = [make_jpeg(f"{i}.jpg") for i in range(3)]
    output = tmp_path / "result.yaml"
    assert main([*(str(p) for p in paths), "-o", str(output)]) == 1
    assert [item["file"]["name"] for item in yaml.safe_load(output.read_text())["images"]] == [str(paths[0])]
    assert 2 <= analyze.call_count <= 3


@pytest.mark.parametrize("flag", ["-a", "-o"])
def test_cli_output_cannot_replace_input_or_db(make_jpeg, tmp_path, flag):
    image = make_jpeg()
    assert main([str(image), flag, str(image)]) == 2
    db = tmp_path / "geonames.db"
    db.write_bytes(b"database")
    assert main([str(image), flag, str(db), "--geonames-db", str(db)]) == 2
    assert db.read_bytes() == b"database"


@pytest.mark.parametrize("retries", [0, 2, 5])
def test_cli_retries_option(monkeypatch, make_jpeg, tmp_path, analysis, retries):
    monkeypatch.setenv("LENZCONTEXT_MODEL", "mock-vision")
    def analyze(self, *args):
        assert self.settings.retries == retries
        return analysis
    monkeypatch.setattr("lenzcontext.llm.openai_compatible.OpenAICompatibleAnalyzer.analyze", analyze)
    image = make_jpeg()
    assert main([str(image), "--retries", str(retries), "-o", str(tmp_path / "out.yaml")]) == 0


@pytest.mark.parametrize("value", ["-1", "not-a-number"])
def test_cli_invalid_retries(monkeypatch, make_jpeg, tmp_path, value):
    monkeypatch.setenv("LENZCONTEXT_MODEL", "mock-vision")
    args = [str(make_jpeg()), "--retries", value, "-o", str(tmp_path / "out.yaml")]
    if value == "-1":
        assert main(args) == 2
    else:
        with pytest.raises(SystemExit) as raised:
            main(args)
        assert raised.value.code == 2
    assert not (tmp_path / "out.yaml").exists()
