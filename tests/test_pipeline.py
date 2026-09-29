import logging
import sqlite3
from unittest.mock import Mock

import pytest
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
    assert [r.file.name for r in batch.images] == ["C.jpg", "A.JPG", "B.JPEG"]
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
