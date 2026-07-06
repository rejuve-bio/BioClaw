from __future__ import annotations

from pathlib import Path
from typing import Any

try:
    import yaml
except ModuleNotFoundError as exc:  # pragma: no cover - environment dependent
    yaml = None
    _IMPORT_ERROR = exc
else:
    _IMPORT_ERROR = None


def load_yaml(path: str | Path) -> Any:
    if yaml is None:
        raise RuntimeError(
            "PyYAML is required to read BioCypher schema/config files. "
            "Install this package with `python3 -m pip install -e .`, or install PyYAML in your active environment."
        ) from _IMPORT_ERROR
    return yaml.safe_load(Path(path).read_text())
