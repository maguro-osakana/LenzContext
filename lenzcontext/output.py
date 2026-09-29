"""Unicode-preserving YAML serialization and per-image output."""

import os
import tempfile
from pathlib import Path

import yaml
from pydantic import ValidationError

from .models import BatchResult, ImageResult


class _Dumper(yaml.SafeDumper):
    pass


def _string(dumper: yaml.SafeDumper, value: str):
    return dumper.represent_scalar("tag:yaml.org,2002:str", value, style="|" if "\n" in value else None)


_Dumper.add_representer(str, _string)


def _dump(value: object) -> str:
    return yaml.dump(value, Dumper=_Dumper, allow_unicode=True, sort_keys=False, width=100)


class InvalidOutput(ValueError):
    """An existing output cannot safely receive more image records."""


class IncrementalYamlWriter:
    def __init__(self, destination: Path, *, append: bool = False):
        self.destination = Path(destination)
        self.started = False
        if append and self.destination.exists() and self.destination.stat().st_size:
            try:
                existing = self.destination.read_text(encoding="utf-8")
                data = yaml.safe_load(existing)
                if not isinstance(data, dict) or set(data) != {"version", "images"}:
                    raise InvalidOutput("existing output must contain version and images")
                batch = BatchResult.model_validate(data)
                if existing != _dump(batch.model_dump(mode="json")):
                    raise InvalidOutput("existing output is not in the generated YAML format")
            except (UnicodeError, yaml.YAMLError, ValidationError) as exc:
                raise InvalidOutput("existing output is not a valid LenzContext YAML file") from exc
            self.started = True

    def write(self, image: ImageResult) -> None:
        # Render the complete record before opening the destination. Each close
        # flushes the record so tail can display it as soon as it is written.
        rendered = _dump({"images": [image.model_dump(mode="json")]})
        record = rendered.removeprefix("images:\n")
        if self.started:
            with self.destination.open("a", encoding="utf-8") as stream:
                stream.write(record)
        else:
            with self.destination.open("w", encoding="utf-8") as stream:
                stream.write("version: 1\nimages:\n" + record)
            self.started = True


def write_yaml(batch: BatchResult, destination: Path) -> None:
    destination = Path(destination)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=destination.parent,
            prefix=f".{destination.name}.", suffix=".tmp", delete=False,
        ) as stream:
            temporary = Path(stream.name)
            stream.write(_dump(batch.model_dump(mode="json")))
        os.replace(temporary, destination)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
