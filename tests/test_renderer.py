"""Tests for SVG floor-plan rendering."""

import importlib
import json
import sys
from io import BytesIO
from pathlib import Path
from types import ModuleType

import pytest
from PIL import Image, ImageChops

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
ElementAssignment = _renderer_module.ElementAssignment

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


def test_renders_text_in_png(tmp_path: Path) -> None:
    source = """\
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 120 60">
  <rect width="120" height="60" fill="white"/>
  <g data-ha-entity="sensor.room_temperature">
    <text id="temperature" data-ha-role="value" x="60" y="40"
          text-anchor="middle" font-family="sans-serif" font-size="24"
          fill="black">—</text>
  </g>
</svg>
"""
    _, png = _renderer(tmp_path, source).render({"sensor.room_temperature": "22"})

    image = Image.open(BytesIO(png)).convert("RGB")
    text_region = image.crop((20, 10, 100, 50))
    assert ImageChops.difference(
        text_region, Image.new("RGB", text_region.size, "white")
    ).getbbox()


def test_renders_glow_with_transparent_edges(tmp_path: Path) -> None:
    source = """\
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100">
  <g data-ha-entity="sensor.room_temperature">
    <rect id="glow" data-ha-role="glow" width="100" height="100"/>
  </g>
</svg>
"""
    svg, png = _renderer(tmp_path, source).render(
        {"sensor.room_temperature": "22"}
    )

    assert b"data-ha-role=\"glow\"" in svg
    assert b"stop-opacity=\"0.8\"" in svg
    assert b"stop-opacity=\"0\"" in svg
    assert b"fill: url(#ha-temperature-glow-glow)" in svg
    image = Image.open(BytesIO(png)).convert("RGBA")
    assert image.getpixel((50, 50))[3] > 0
    assert image.getpixel((0, 50))[3] == 0


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
    renderer = _renderer(
        tmp_path,
        '<svg xmlns="http://www.w3.org/2000/svg">'
        '<rect id="room" width="100" height="100"/></svg>',
    )

    assert renderer.entity_ids == ()
    assert [(element.element_id, element.tag) for element in renderer.assignable_elements] == [
        ("room", "rect")
    ]


def test_assignable_elements_use_svg_editor_labels(tmp_path: Path) -> None:
    source = """\
<svg xmlns="http://www.w3.org/2000/svg"
     xmlns:inkscape="http://www.inkscape.org/namespaces/inkscape">
  <g inkscape:label="Rooms">
    <rect id="room" inkscape:label="Living room"/>
    <rect id="hall"/>
  </g>
</svg>
"""
    renderer = _renderer(tmp_path, source)

    assert renderer.assignable_elements == (
        _renderer_module.AssignableElement("room", "rect", "Living room"),
        _renderer_module.AssignableElement("hall", "rect", "Rooms"),
    )


def test_assigns_entity_to_svg_element_without_metadata(tmp_path: Path) -> None:
    source = """\
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100">
  <rect id="room" width="100" height="100" fill="white"/>
</svg>
"""
    svg_path = tmp_path / "floorplan.svg"
    svg_path.write_text(source, encoding="utf-8")
    renderer = FloorplanRenderer(
        svg_path,
        (ElementAssignment("room", "sensor.room_temperature", "area"),),
    )

    svg, png = renderer.render({"sensor.room_temperature": "22"})

    assert renderer.entity_ids == ("sensor.room_temperature",)
    assert b'fill: #22C55E' in svg
    assert png.startswith(b"\x89PNG\r\n\x1a\n")


def test_displays_multiple_sensors_and_colors_with_separate_sensor(
    tmp_path: Path,
) -> None:
    source = """\
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100">
  <rect id="room" width="100" height="100"/>
  <text id="room-values" x="10" y="20"/>
</svg>
"""
    svg_path = tmp_path / "floorplan.svg"
    svg_path.write_text(source, encoding="utf-8")
    renderer = FloorplanRenderer(
        svg_path,
        (
            ElementAssignment(
                "room",
                display_entity_ids=(),
                color_entity_id="sensor.temperature",
                color_mode="fill",
            ),
            ElementAssignment(
                "room-values",
                display_entity_ids=(
                    "sensor.temperature",
                    "sensor.humidity",
                ),
            ),
        ),
    )

    svg, _ = renderer.render(
        {"sensor.temperature": "22", "sensor.humidity": "45"},
        {
            "sensor.temperature": "Living temperature: 22 °C",
            "sensor.humidity": "Living humidity: 45 %",
        },
    )

    assert set(renderer.entity_ids) == {
        "sensor.temperature",
        "sensor.humidity",
    }
    assert b"#22C55E" in svg
    assert b"Living temperature: 22 \xc2\xb0C" in svg
    assert b"Living humidity: 45 %" in svg
    assert b":tspan" in svg


def test_configured_svg_reloads_sensor_choices_without_config_flow(
    tmp_path: Path,
) -> None:
    source = """\
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100">
  <rect id="room" width="100" height="100"/>
  <text id="room-values" x="10" y="20"/>
</svg>
"""
    source_path = tmp_path / "floorplan.svg"
    source_path.write_text(source, encoding="utf-8")
    assignments = (
        ElementAssignment(
            "room",
            color_entity_id="sensor.temperature",
            color_mode="fade",
        ),
        ElementAssignment(
            "room-values",
            display_entity_ids=("sensor.temperature", "sensor.humidity"),
        ),
    )

    configured_path = tmp_path / "floorplan.configured.svg"
    configured_path.write_bytes(
        FloorplanRenderer(source_path).configured_svg(assignments)
    )
    renderer = FloorplanRenderer(configured_path)
    svg, _ = renderer.render(
        {"sensor.temperature": "22", "sensor.humidity": "45"},
        {
            "sensor.temperature": "Temperature: 22 °C",
            "sensor.humidity": "Humidity: 45 %",
        },
    )

    assert set(renderer.entity_ids) == {
        "sensor.temperature",
        "sensor.humidity",
    }
    assert b"data-ha-color-mode=\"fade\"" in svg
    assert b"data-ha-display-entities=\"sensor.temperature,sensor.humidity\"" in svg
    assert b"Temperature: 22 \xc2\xb0C" in svg
    assert b"Humidity: 45 %" in svg


def test_renders_svg_with_physical_dimensions(tmp_path: Path) -> None:
    source = """\
<svg xmlns="http://www.w3.org/2000/svg" width="10mm" height="5mm"
     viewBox="0 0 10 5">
  <rect id="room" width="10" height="5"/>
</svg>
"""
    svg_path = tmp_path / "floorplan.svg"
    svg_path.write_text(source, encoding="utf-8")
    renderer = FloorplanRenderer(
        svg_path,
        (ElementAssignment("room", "sensor.room_temperature", "area"),),
    )

    _, png = renderer.render({"sensor.room_temperature": "22"})

    assert Image.open(BytesIO(png)).size == (38, 19)


def test_rejects_assignment_to_unknown_svg_element(tmp_path: Path) -> None:
    source = '<svg xmlns="http://www.w3.org/2000/svg"><rect id="room"/></svg>'
    svg_path = tmp_path / "floorplan.svg"
    svg_path.write_text(source, encoding="utf-8")

    with pytest.raises(FloorplanError, match="cannot be assigned"):
        FloorplanRenderer(
            svg_path,
            (ElementAssignment("missing", "sensor.room_temperature", "area"),),
        )


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
