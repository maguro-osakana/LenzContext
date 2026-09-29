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
