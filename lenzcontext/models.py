"""Validated internal and output records."""

from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)


class ExifInfo(Model):
    taken_at: str | None = None
    latitude: float | None = Field(default=None, ge=-90, le=90)
    longitude: float | None = Field(default=None, ge=-180, le=180)


class Address(Model):
    english: str
    local: str


class OCRResult(Model):
    detected: bool
    text: str

    @field_validator("text", mode="after")
    @classmethod
    def normalize_whitespace(cls, text: str) -> str:
        return " ".join(text.split())

    @model_validator(mode="after")
    def consistent_detection(self) -> Self:
        if self.detected != bool(self.text.strip()):
            raise ValueError("OCR detected must agree with nonempty text")
        if not self.detected and self.text != "":
            raise ValueError("OCR without detection must have empty text")
        return self


class AnalysisResult(Model):
    description_en: str = Field(min_length=1)
    description: str = Field(min_length=1)
    ocr: OCRResult
    screenshot_probability: float = Field(ge=0.0, le=1.0)


class FileInfo(Model):
    name: str


class ImageResult(Model):
    file: FileInfo
    exif: ExifInfo
    address: Address | None
    analysis: AnalysisResult


class BatchResult(Model):
    version: Literal[1] = 1
    images: list[ImageResult]
