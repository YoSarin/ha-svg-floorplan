"""JSON persistence for named SVG render configurations."""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any

from .const import TIMESTAMP_POSITIONS
from .renderer import ElementAssignment, FloorplanError

CONFIGURATION_VERSION = 1


def configuration_path(source_path: Path) -> Path:
    """Return the JSON sidecar path for an SVG source."""
    return source_path.with_suffix(".json")


def _read_configuration_file(source_path: Path) -> dict[str, Any]:
    path = configuration_path(source_path)
    try:
        data: Any = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as err:
        raise FloorplanError(f"Cannot read configuration JSON {path}: {err}") from err
    if (
        not isinstance(data, dict)
        or data.get("version") != CONFIGURATION_VERSION
        or not isinstance(data.get("configurations"), dict)
    ):
        raise FloorplanError(f"Configuration JSON {path} has an unsupported format")
    return data


def load_configuration(
    source_path: Path, name: str
) -> tuple[ElementAssignment, ...]:
    """Load one named configuration from its SVG's JSON sidecar."""
    path = configuration_path(source_path)
    data = _read_configuration_file(source_path)
    configuration = data["configurations"].get(name)
    if not isinstance(configuration, dict) or not isinstance(
        configuration.get("assignments"), list
    ):
        raise FloorplanError(
            f"Configuration {name!r} is missing from JSON file {path}"
        )
    assignments = []
    try:
        for item in configuration["assignments"]:
            if not isinstance(item, dict):
                raise TypeError("each assignment must be an object")
            assignment = dict(item)
            if "improve_readability" not in assignment and "text_shadow" in assignment:
                assignment["improve_readability"] = assignment["text_shadow"]
            assignment.pop("text_shadow", None)
            for key in ("display_entity_ids", "device_ids"):
                assignment[key] = tuple(assignment.get(key, ()))
            assignments.append(ElementAssignment(**assignment))
    except (TypeError, ValueError) as err:
        raise FloorplanError(
            f"Configuration {name!r} in {path} is invalid: {err}"
        ) from err
    return tuple(assignments)


def load_timestamp_position(source_path: Path, name: str) -> str | None:
    """Load and validate the optional snapshot timestamp position."""
    data = _read_configuration_file(source_path)
    configuration = data["configurations"].get(name)
    if not isinstance(configuration, dict):
        raise FloorplanError(
            f"Configuration {name!r} is missing from JSON file "
            f"{source_path.with_suffix('.json')}"
        )
    position = configuration.get("timestamp_position")
    if position is not None and position not in TIMESTAMP_POSITIONS:
        raise FloorplanError(
            f"Configuration {name!r} has an invalid timestamp position"
        )
    return position


def save_configuration(
    source_path: Path,
    name: str,
    assignments: tuple[ElementAssignment, ...],
    timestamp_position: str | None = None,
) -> None:
    """Persist one configuration, preserving other configurations for the SVG."""
    path = configuration_path(source_path)
    if path.exists():
        data = _read_configuration_file(source_path)
    else:
        data = {"version": CONFIGURATION_VERSION, "configurations": {}}

    if timestamp_position is not None and timestamp_position not in TIMESTAMP_POSITIONS:
        raise FloorplanError("Configuration has an invalid timestamp position")
    data["configurations"][name] = {
        "assignments": [asdict(assignment) for assignment in assignments],
        "timestamp_position": timestamp_position,
    }
    temporary_path: Path | None = None
    try:
        with NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as temporary_file:
            temporary_path = Path(temporary_file.name)
            json.dump(data, temporary_file, indent=2, ensure_ascii=False)
            temporary_file.write("\n")
        temporary_path.replace(path)
    except OSError:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
        raise
