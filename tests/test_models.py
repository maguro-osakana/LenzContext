import pytest
from pydantic import ValidationError

from lenzcontext.models import AnalysisResult, OCRResult


@pytest.mark.parametrize("value", [-0.1, 1.1, float("nan"), float("inf"), "0.5", True])
def test_invalid_probability(analysis, value):
    data = analysis.model_dump()
    data["screenshot_probability"] = value
    with pytest.raises(ValidationError):
        AnalysisResult.model_validate(data)


@pytest.mark.parametrize("value", [0, 0.5, 1])
def test_valid_probability(analysis, value):
    data = analysis.model_dump()
    data["screenshot_probability"] = value
    assert AnalysisResult.model_validate(data).screenshot_probability == value


def test_ocr_consistency():
    assert OCRResult(detected=False, text="").text == ""
    with pytest.raises(ValidationError):
        OCRResult(detected=False, text="visible")
    with pytest.raises(ValidationError):
        OCRResult(detected=True, text="")


@pytest.mark.parametrize("space", [" ", "  ", "\n", "\r\n", "\r", "\t", "\v", "\f",
                                  "\u3000", "\u00a0", "\u2009", "\u2028", "\u2029"])
def test_ocr_whitespace_normalized(space):
    result = OCRResult(detected=True, text=f"{space}서울역{space}Welcome{space}出口{space}")
    assert result.text == "서울역 Welcome 出口"


def test_ocr_mixed_whitespace_and_original_characters():
    assert OCRResult(detected=True, text="  서울역\r\nWelcome\t\t出口　案内  ").text == "서울역 Welcome 出口 案内"
    original = "Café e\u0301 ＡＢＣ、出口!"
    assert OCRResult(detected=True, text=original).text == original


@pytest.mark.parametrize("text", [" ", "\r\n\t　\u00a0"])
def test_ocr_whitespace_only_detection(text):
    assert OCRResult(detected=False, text=text).text == ""
    with pytest.raises(ValidationError):
        OCRResult(detected=True, text=text)


@pytest.mark.parametrize("text", [None, 123, True, [], b"text"])
def test_ocr_normalization_preserves_strict_type_validation(text):
    with pytest.raises(ValidationError):
        OCRResult(detected=True, text=text)
