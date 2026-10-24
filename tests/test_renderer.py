"""Tests for SVG floor-plan rendering."""

import importlib
import json
import sys
from pathlib import Path
from types import ModuleType

import pytest

# Load the pure renderer without importing Home Assistant integration modules.
_package_name = "custom_components.svg_floorplan"
_package = ModuleType(_package_name)
_package.__path__ = [
    str(Path(__file__).parents[1] / "custom_components" / "svg_floorplan")
]
sys.modules.setdefault(_package_name, _package)
_renderer_module = importlib.import_module(
    "custom_components.svg_floorplan.renderer"
)
FloorplanError = _renderer_module.FloorplanError
FloorplanRenderer = _renderer_module.FloorplanRenderer

SAMPLE_SVG = """\
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100">
  <g data-ha-entity="sensor.room_temperature">
    <rect id="room" data-ha-role="area" width="100" height="100"
          fill="#000000" style="stroke: black"/>
    <text id="temperature" data-ha-role="value" x="50" y="50">—</text>
  </g>
</svg>
"""


def _renderer(tmp_path: Path, source: str = SAMPLE_SVG) -> FloorplanRenderer:
    svg_path = tmp_path / "floorplan.svg"
    svg_path.write_text(source, encoding="utf-8")
    return FloorplanRenderer(svg_path)


def test_renders_value_and_interpolated_color(tmp_path: Path) -> None:
    renderer = _renderer(tmp_path)

    svg, png = renderer.render({"sensor.room_temperature": "22"})

    assert b"22 \xc2\xb0C" in svg
    assert b"#22C55E" in svg
    assert png.startswith(b"\x89PNG\r\n\x1a\n")


def test_uses_unavailable_label_and_neutral_color(tmp_path: Path) -> None:
    renderer = _renderer(tmp_path)

    svg, _ = renderer.render({"sensor.room_temperature": "unknown"})

    assert "—".encode() in svg
    assert b"#808080" in svg


@pytest.mark.parametrize(
    "metadata",
    [
        'data-ha-min="22" data-ha-comfort="22" data-ha-max="28"',
        'data-ha-color-cold="url(#paint)"',
    ],
)
def test_rejects_invalid_room_metadata(tmp_path: Path, metadata: str) -> None:
    source = SAMPLE_SVG.replace(
        'data-ha-entity="sensor.room_temperature"',
        f'data-ha-entity="sensor.room_temperature" {metadata}',
    )

    with pytest.raises(FloorplanError):
        _renderer(tmp_path, source)


def test_rejects_svg_without_entity_bindings(tmp_path: Path) -> None:
    with pytest.raises(FloorplanError, match="no elements with data-ha-entity"):
        _renderer(tmp_path, '<svg xmlns="http://www.w3.org/2000/svg"/>')


def test_example_svg_has_both_sensor_bindings(tmp_path: Path) -> None:
    example = Path(__file__).parents[1] / "examples" / "two_rooms.svg"
    renderer = FloorplanRenderer(example)

    svg, png = renderer.render(
        {
            "sensor.teplomer_obyvak_temperature": "21.5",
            "sensor.teplomer_venku_temperature": "8.2",
        }
    )

    assert set(renderer.entity_ids) == {
        "sensor.teplomer_obyvak_temperature",
        "sensor.teplomer_venku_temperature",
    }
    assert b"21.5 \xc2\xb0C" in svg
    assert b"8.2 \xc2\xb0C" in svg
    assert png.startswith(b"\x89PNG\r\n\x1a\n")


def test_rejects_duplicate_svg_ids(tmp_path: Path) -> None:
    duplicate = SAMPLE_SVG.replace(
        'id="temperature"', 'id="room"'
    )
    with pytest.raises(FloorplanError, match="IDs must be unique"):
        _renderer(tmp_path, duplicate)


def test_custom_integration_includes_english_translations() -> None:
    integration = Path(__file__).parents[1] / "custom_components" / "svg_floorplan"
    source = json.loads((integration / "strings.json").read_text(encoding="utf-8"))
    translation = json.loads(
        (integration / "translations" / "en.json").read_text(encoding="utf-8")
    )

    assert translation == source


def test_manifest_classifies_floorplan_as_service() -> None:
    manifest_path = (
        Path(__file__).parents[1]
        / "custom_components"
        / "svg_floorplan"
        / "manifest.json"
    )
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    assert manifest["integration_type"] == "service"
    assert manifest["iot_class"] == "calculated"
