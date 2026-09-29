"""Atomic, Unicode-preserving YAML serialization."""

import os
import tempfile
from pathlib import Path

import yaml

from .models import BatchResult


class _Dumper(yaml.SafeDumper):
    pass


def _string(dumper: yaml.SafeDumper, value: str):
    return dumper.represent_scalar("tag:yaml.org,2002:str", value, style="|" if "\n" in value else None)


_Dumper.add_representer(str, _string)


def write_yaml(batch: BatchResult, destination: Path) -> None:
    destination = Path(destination)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=destination.parent,
            prefix=f".{destination.name}.", suffix=".tmp", delete=False,
        ) as stream:
            temporary = Path(stream.name)
            yaml.dump(batch.model_dump(mode="json"), stream, Dumper=_Dumper,
                      allow_unicode=True, sort_keys=False, width=100)
        os.replace(temporary, destination)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
