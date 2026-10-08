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


def path_key(name: str, base_dir: str) -> str:
    """Normalize names against the execution directory, without resolving symlinks."""
    return os.path.normpath(os.path.join(base_dir, name))


def _existing_images(destination: Path):
    """Read one document without retaining the entire images sequence."""
    try:
        with destination.open(encoding="utf-8-sig") as stream:
            loader = yaml.SafeLoader(stream)
            try:
                def expect(event_type):
                    if not isinstance(loader.get_event(), event_type):
                        raise InvalidOutput("unexpected YAML structure")

                def value():
                    node = loader.compose_node(None, None)
                    result = loader.construct_document(node)
                    loader.anchors.clear()
                    return result

                expect(yaml.StreamStartEvent)
                expect(yaml.DocumentStartEvent)
                expect(yaml.MappingStartEvent)
                seen = set()
                while not loader.check_event(yaml.MappingEndEvent):
                    expect_key = loader.peek_event()
                    if not isinstance(expect_key, yaml.ScalarEvent):
                        raise InvalidOutput("expected version or images")
                    key = value()
                    if not isinstance(key, str) or key not in {"version", "images"} or key in seen:
                        raise InvalidOutput("expected unique version and images fields")
                    seen.add(key)
                    if key == "version":
                        BatchResult.model_validate({"version": value(), "images": []})
                    else:
                        expect(yaml.SequenceStartEvent)
                        while not loader.check_event(yaml.SequenceEndEvent):
                            yield ImageResult.model_validate(value())
                        expect(yaml.SequenceEndEvent)
                if seen != {"version", "images"}:
                    raise InvalidOutput("existing output must contain version and images")
                expect(yaml.MappingEndEvent)
                expect(yaml.DocumentEndEvent)
                expect(yaml.StreamEndEvent)
            finally:
                loader.dispose()
    except (UnicodeError, yaml.YAMLError, ValidationError, ValueError, TypeError) as exc:
        raise InvalidOutput("existing output is not a valid LenzContext YAML file") from exc


def read_existing_names(destination: Path, base_dir: str) -> set[str]:
    if not destination.exists() or not destination.stat().st_size:
        return set()
    return {path_key(image.file.name, base_dir) for image in _existing_images(destination)}


def _normalize_existing(destination: Path, base_dir: str | None) -> tuple[set[str], bool]:
    """Replace the original only after the complete document validates."""
    names: set[str] = set()
    populated = False
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=destination.parent,
            prefix=f".{destination.name}.", suffix=".tmp", delete=False,
        ) as stream:
            temporary = Path(stream.name)
            stream.write("version: 1\nimages:\n")
            for image in _existing_images(destination):
                stream.write(_dump({"images": [image.model_dump(mode="json")]}).removeprefix("images:\n"))
                populated = True
                if base_dir is not None:
                    names.add(path_key(image.file.name, base_dir))
            if not populated:
                stream.seek(0)
                stream.truncate()
                stream.write("version: 1\nimages: []\n")
        os.chmod(temporary, destination.stat().st_mode & 0o777)
        os.replace(temporary, destination)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return names, populated


class IncrementalYamlWriter:
    def __init__(self, destination: Path, *, append: bool = False, resume_base_dir: str | None = None):
        self.destination = Path(destination)
        self.started = False
        self.existing_names: set[str] = set()
        if (append or resume_base_dir is not None) and self.destination.exists() and self.destination.stat().st_size:
            self.existing_names, self.started = _normalize_existing(self.destination, resume_base_dir)

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
