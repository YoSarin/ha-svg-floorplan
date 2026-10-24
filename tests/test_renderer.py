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
    assert b"stop-opacity=\"1\"" in svg
    assert b"stop-opacity=\"0.9\"" in svg
    assert b"stop-opacity=\"0.45\"" in svg
    assert b"stop-opacity=\"0\"" in svg
    assert b"fill: url(#ha-temperature-glow-glow)" in svg
    image = Image.open(BytesIO(png)).convert("RGBA")
    assert image.getpixel((50, 50))[3] > 0
    assert image.getpixel((75, 50))[3] >= 200
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
  <rect id="room" x="20" y="20" width="30" height="40"/>
</svg>
"""
    svg_path = tmp_path / "floorplan.svg"
    svg_path.write_text(source, encoding="utf-8")
    renderer = FloorplanRenderer(
        svg_path,
        (
            ElementAssignment(
                "room",
                display_entity_ids=(
                    "sensor.temperature",
                    "sensor.humidity",
                ),
                color_entity_id="sensor.temperature",
                color_mode="fill",
            ),
        ),
    )

    svg, _ = renderer.render(
        {"sensor.temperature": "22", "sensor.humidity": "45"},
        {
            "sensor.temperature": "22 °C",
            "sensor.humidity": "45 %",
        },
    )

    assert set(renderer.entity_ids) == {
        "sensor.temperature",
        "sensor.humidity",
    }
    assert b"#22C55E" in svg
    assert b"22 \xc2\xb0C" in svg
    assert b"45 %" in svg
    assert b"Living" not in svg
    assert b":tspan" in svg
    root = _renderer_module.ET.fromstring(svg)
    overlay = next(
        element
        for element in root.iter()
        if element.tag.endswith("text")
    )
    assert overlay.attrib["x"] == "35"
    assert overlay.attrib["y"] == "40"
    assert [
        span.text for span in overlay if span.tag.endswith("tspan")
    ] == ["22 °C", "45 %"]


def test_auto_places_text_at_edge_of_canvas_sized_shape(tmp_path: Path) -> None:
    source = """\
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 80">
  <rect id="backdrop" width="100" height="80"/>
</svg>
"""
    svg_path = tmp_path / "floorplan.svg"
    svg_path.write_text(source, encoding="utf-8")
    renderer = FloorplanRenderer(
        svg_path,
        (
            ElementAssignment(
                "backdrop",
                display_entity_ids=("sensor.outdoor_temperature",),
            ),
        ),
    )

    svg, _ = renderer.render(
        {"sensor.outdoor_temperature": "12"},
        {"sensor.outdoor_temperature": "12 °C"},
    )
    root = _renderer_module.ET.fromstring(svg)
    overlay = next(element for element in root.iter() if element.tag.endswith("text"))

    assert overlay.attrib["text-anchor"] == "start"
    assert 0 < float(overlay.attrib["x"]) < 10
    assert 0 < float(overlay.attrib["y"]) < 20


def test_centers_generated_text_on_path_area_centroid(tmp_path: Path) -> None:
    source = """\
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 60">
  <path id="attic" d="M 0 0 L 80 0 L 60 40 L 20 40 Z"/>
</svg>
"""
    svg_path = tmp_path / "floorplan.svg"
    svg_path.write_text(source, encoding="utf-8")
    renderer = FloorplanRenderer(
        svg_path,
        (ElementAssignment("attic", display_entity_ids=("sensor.temperature",)),),
    )

    svg, _ = renderer.render(
        {"sensor.temperature": "22"},
        {"sensor.temperature": "22 °C"},
    )
    root = _renderer_module.ET.fromstring(svg)
    overlay = next(element for element in root.iter() if element.tag.endswith("text"))

    assert float(overlay.attrib["x"]) == pytest.approx(40)
    assert float(overlay.attrib["y"]) == pytest.approx(17.7778, abs=0.001)


def test_scales_generated_text_to_fit_shape(tmp_path: Path) -> None:
    source = """\
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 40">
  <rect id="room" x="10" y="10" width="30" height="20"/>
</svg>
"""
    svg_path = tmp_path / "floorplan.svg"
    svg_path.write_text(source, encoding="utf-8")
    renderer = FloorplanRenderer(
        svg_path,
        (ElementAssignment("room", display_entity_ids=("sensor.temperature",)),),
    )

    svg, _ = renderer.render(
        {"sensor.temperature": "22"},
        {
            "sensor.temperature": (
                "A very long sensor reading that should scale down"
            )
        },
    )
    root = _renderer_module.ET.fromstring(svg)
    overlay = next(element for element in root.iter() if element.tag.endswith("text"))
    font_size = float(overlay.attrib["font-size"])

    assert font_size < 20 * 0.14
    assert font_size * 1.2 <= 20 * 0.84


def test_configured_svg_reloads_sensor_choices_without_config_flow(
    tmp_path: Path,
) -> None:
    source = """\
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100">
  <rect id="room" width="100" height="100"/>
</svg>
"""
    source_path = tmp_path / "floorplan.svg"
    source_path.write_text(source, encoding="utf-8")
    assignments = (
        ElementAssignment(
            "room",
            display_entity_ids=("sensor.temperature", "sensor.humidity"),
            display_position="top-right",
            color_entity_id="sensor.temperature",
            color_mode="fade",
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
            "sensor.temperature": "22 °C",
            "sensor.humidity": "45 %",
        },
    )

    assert set(renderer.entity_ids) == {
        "sensor.temperature",
        "sensor.humidity",
    }
    assert b"data-ha-color-mode=\"fade\"" in svg
    assert b"data-ha-display-entities=\"sensor.temperature,sensor.humidity\"" in svg
    assert b"data-ha-display-position=\"top-right\"" in svg
    assert b"22 \xc2\xb0C" in svg
    assert b"45 %" in svg


def test_skipped_elements_use_neutral_color_and_persist_in_configured_svg(
    tmp_path: Path,
) -> None:
    source = """\
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100">
  <rect id="room" width="50" height="50" fill="yellow" stroke="black"/>
  <text id="label" x="10" y="70" fill="black">Unused</text>
</svg>
"""
    source_path = tmp_path / "floorplan.svg"
    source_path.write_text(source, encoding="utf-8")
    assignments = (
        ElementAssignment("room", neutralize=True),
        ElementAssignment("label", neutralize=True),
    )
    configured_path = tmp_path / "floorplan.configured.svg"
    configured_path.write_bytes(
        FloorplanRenderer(source_path).configured_svg(assignments)
    )

    renderer = FloorplanRenderer(configured_path)
    svg, _ = renderer.render({})
    root = _renderer_module.ET.fromstring(svg)
    elements = {element.attrib.get("id"): element for element in root.iter()}

    assert elements["room"].attrib["fill"] == "#808080"
    assert elements["room"].attrib["stroke"] == "black"
    assert elements["label"].attrib["fill"] == "#404040"
    assert b'data-ha-neutral="true"' in svg


def test_device_selection_persists_per_svg_element(tmp_path: Path) -> None:
    source = """\
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100">
  <rect id="room_a" width="40" height="40"/>
  <rect id="room_b" x="50" width="40" height="40"/>
</svg>
"""
    source_path = tmp_path / "floorplan.svg"
    source_path.write_text(source, encoding="utf-8")
    assignments = (
        ElementAssignment("room_a", device_ids=("device_a", "device_b")),
        ElementAssignment("room_b", device_ids=("device_c",), neutralize=True),
    )

    configured_path = tmp_path / "floorplan.configured.svg"
    configured_path.write_bytes(
        FloorplanRenderer(source_path).configured_svg(assignments)
    )
    renderer = FloorplanRenderer(configured_path)

    assert {
        assignment.element_id: assignment.device_ids
        for assignment in renderer.assignments
    } == {"room_a": ("device_a", "device_b"), "room_b": ("device_c",)}


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


def test_example_house_svg_renders_and_exposes_labeled_areas(tmp_path: Path) -> None:
    example = Path(__file__).parents[1] / "examples" / "example_house.svg"
    renderer = FloorplanRenderer(example)

    svg, png = renderer.render({})

    assert renderer.entity_ids == ()
    assert len(renderer.assignable_elements) == 10
    assert any(element.label == "Obývák" for element in renderer.assignable_elements)
    assert b"data:image/png;base64," in svg
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
