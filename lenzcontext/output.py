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


def read_existing_names(destination: Path, base_dir: str) -> set[str]:
    """Validate generated YAML per image, retaining only normalized names."""
    names: set[str] = set()
    if not destination.exists() or not destination.stat().st_size:
        return names
    try:
        with destination.open(encoding="utf-8") as stream, destination.open(encoding="utf-8") as canonical:
            loader = yaml.SafeLoader(stream)
            try:
                def expect(event_type, value=None):
                    event = loader.get_event()
                    if (not isinstance(event, event_type)
                            or (value is not None and event.value != value)):
                        raise InvalidOutput("existing output is not in the generated YAML format")

                def match(text):
                    if canonical.read(len(text)) != text:
                        raise InvalidOutput("existing output is not in the generated YAML format")

                expect(yaml.StreamStartEvent)
                expect(yaml.DocumentStartEvent)
                expect(yaml.MappingStartEvent)
                expect(yaml.ScalarEvent, "version")
                expect(yaml.ScalarEvent, "1")
                expect(yaml.ScalarEvent, "images")
                expect(yaml.SequenceStartEvent)
                if loader.check_event(yaml.SequenceEndEvent):
                    match("version: 1\nimages: []\n")
                else:
                    match("version: 1\nimages:\n")
                while not loader.check_event(yaml.SequenceEndEvent):
                    node = loader.compose_node(None, None)
                    image = ImageResult.model_validate(loader.construct_document(node))
                    match(_dump({"images": [image.model_dump(mode="json")]}).removeprefix("images:\n"))
                    names.add(path_key(image.file.name, base_dir))
                    loader.anchors.clear()
                expect(yaml.SequenceEndEvent)
                expect(yaml.MappingEndEvent)
                expect(yaml.DocumentEndEvent)
                expect(yaml.StreamEndEvent)
                if canonical.read(1):
                    raise InvalidOutput("existing output is not in the generated YAML format")
            finally:
                loader.dispose()
    except (UnicodeError, yaml.YAMLError, ValidationError, ValueError, TypeError) as exc:
        raise InvalidOutput("existing output is not a valid LenzContext YAML file") from exc
    return names


class IncrementalYamlWriter:
    def __init__(self, destination: Path, *, append: bool = False, resume_base_dir: str | None = None):
        self.destination = Path(destination)
        self.started = False
        self.existing_names: set[str] = set()
        if resume_base_dir is not None:
            self.existing_names = read_existing_names(self.destination, resume_base_dir)
            self.started = bool(self.existing_names)
            return
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
