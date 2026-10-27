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
_configuration_module = importlib.import_module(
    "custom_components.svg_floorplan.configuration"
)
load_configuration = _configuration_module.load_configuration
save_configuration = _configuration_module.save_configuration

SAMPLE_SVG = """\
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100">
  <rect id="room" width="100" height="100"
        fill="#000000" style="stroke: black"/>
  <text id="temperature" x="50" y="50">—</text>
</svg>
"""


def _renderer(
    tmp_path: Path,
    source: str = SAMPLE_SVG,
    assignments: tuple[ElementAssignment, ...] = (
        ElementAssignment(
            "room",
            color_entity_id="sensor.room_temperature",
            color_mode="fill",
        ),
        ElementAssignment(
            "temperature",
            display_entity_ids=("sensor.room_temperature",),
        ),
    ),
) -> FloorplanRenderer:
    svg_path = tmp_path / "floorplan.svg"
    svg_path.write_text(source, encoding="utf-8")
    return FloorplanRenderer(svg_path, assignments)


def test_renders_value_and_interpolated_color(tmp_path: Path) -> None:
    renderer = _renderer(tmp_path)

    svg, png = renderer.render(
        {"sensor.room_temperature": "22"},
        {"sensor.room_temperature": "22 °C"},
    )

    assert b"22 \xc2\xb0C" in svg
    assert b"#E0DA90" in svg
    assert png.startswith(b"\x89PNG\r\n\x1a\n")


def test_renders_text_in_png(tmp_path: Path) -> None:
    source = """\
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 120 60">
  <rect width="120" height="60" fill="white"/>
  <text id="temperature" x="60" y="40"
        text-anchor="middle" font-family="sans-serif" font-size="24"
        fill="black">—</text>
</svg>
"""
    svg_path = tmp_path / "floorplan.svg"
    svg_path.write_text(source, encoding="utf-8")
    renderer = FloorplanRenderer(
        svg_path,
        (ElementAssignment("temperature", display_entity_ids=("sensor.room_temperature",)),),
    )
    _, png = renderer.render(
        {"sensor.room_temperature": "22"},
        {"sensor.room_temperature": "22 °C"},
    )

    image = Image.open(BytesIO(png)).convert("RGB")
    text_region = image.crop((20, 10, 100, 50))
    assert ImageChops.difference(
        text_region, Image.new("RGB", text_region.size, "white")
    ).getbbox()


def test_soft_shadow_replaces_outline_on_existing_text(tmp_path: Path) -> None:
    source = """\
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 120 60">
  <rect width="120" height="60" fill="white"/>
  <text id="temperature" x="60" y="40" text-anchor="middle"
        style="fill: black; stroke: black; stroke-width: 2">—</text>
</svg>
"""
    renderer = _renderer(
        tmp_path,
        source,
        assignments=(
            ElementAssignment(
                "temperature",
                display_entity_ids=("sensor.room_temperature",),
                improve_readability=True,
            ),
        ),
    )

    svg, png = renderer.render(
        {"sensor.room_temperature": "22"},
        {"sensor.room_temperature": "22 °C"},
    )
    root = _renderer_module.ET.fromstring(svg)
    text = next(element for element in root.iter() if element.tag.endswith("text"))
    shadow_filter = next(
        element for element in root.iter() if element.tag.endswith("filter")
    )

    assert text.attrib["stroke"] == "none"
    assert text.attrib["style"] == "fill: black; stroke: none"
    assert text.attrib["filter"] == f"url(#{shadow_filter.attrib['id']})"
    blur_deviations = [
        element.attrib["stdDeviation"]
        for element in shadow_filter
        if element.tag.endswith("feGaussianBlur")
    ]
    assert blur_deviations == ["6.4", "1.6"]
    assert next(
        element.attrib["radius"]
        for element in shadow_filter
        if element.tag.endswith("feMorphology")
    ) == "2.24"
    assert any(
        element.tag.endswith("feFlood")
        and element.attrib["flood-opacity"] == "1"
        for element in shadow_filter
    )
    assert png.startswith(b"\x89PNG\r\n\x1a\n")


def test_soft_shadow_replaces_outline_on_shape_readings(tmp_path: Path) -> None:
    renderer = _renderer(
        tmp_path,
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100">'
        '<rect id="room" width="100" height="100"/></svg>',
        assignments=(
            ElementAssignment(
                "room",
                display_entity_ids=("sensor.room_temperature",),
                improve_readability=True,
            ),
        ),
    )

    svg, png = renderer.render(
        {"sensor.room_temperature": "22"},
        {"sensor.room_temperature": "22 °C"},
    )
    root = _renderer_module.ET.fromstring(svg)
    text = next(element for element in root.iter() if element.tag.endswith("text"))

    assert text.attrib["stroke"] == "none"
    assert "stroke-width" not in text.attrib
    assert "paint-order" not in text.attrib
    assert text.attrib["filter"].startswith("url(#ha-text-shadow-")
    assert b"feGaussianBlur" in svg
    assert png.startswith(b"\x89PNG\r\n\x1a\n")


def test_readability_halo_is_relatively_larger_for_small_text(
    tmp_path: Path,
) -> None:
    source = """\
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100">
  <text id="small" x="10" y="20" font-size="4">—</text>
  <text id="large" x="10" y="60" font-size="16">—</text>
</svg>
"""
    renderer = _renderer(
        tmp_path,
        source,
        assignments=(
            ElementAssignment(
                "small",
                display_entity_ids=("sensor.room_temperature",),
                improve_readability=True,
            ),
            ElementAssignment(
                "large",
                display_entity_ids=("sensor.room_temperature",),
                improve_readability=True,
            ),
        ),
    )

    svg, _ = renderer.render(
        {"sensor.room_temperature": "22"},
        {"sensor.room_temperature": "22 °C"},
    )
    root = _renderer_module.ET.fromstring(svg)
    filters = {item.attrib["id"]: item for item in root.iter() if item.tag.endswith("filter")}
    small_radius = next(
        float(item.attrib["radius"])
        for item in filters["ha-text-shadow-small"]
        if item.tag.endswith("feMorphology")
    )
    large_radius = next(
        float(item.attrib["radius"])
        for item in filters["ha-text-shadow-large"]
        if item.tag.endswith("feMorphology")
    )

    assert small_radius / 4 > large_radius / 16


def test_ignores_embedded_svg_configuration_metadata(tmp_path: Path) -> None:
    source = """\
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100">
  <g data-ha-entity="sensor.room_temperature" data-ha-min="0">
    <rect id="room" data-ha-role="area" width="100" height="100"
          fill="#000000"/>
  </g>
</svg>
"""
    renderer = _renderer(tmp_path, source, assignments=())
    svg, _ = renderer.render({"sensor.room_temperature": "22"})

    assert renderer.entity_ids == ()
    assert b'data-ha-entity="sensor.room_temperature"' in svg
    assert b'fill="#000000"' in svg


def test_uses_unavailable_label_and_neutral_color(tmp_path: Path) -> None:
    renderer = _renderer(tmp_path)

    svg, _ = renderer.render(
        {"sensor.room_temperature": "unknown"},
        {"sensor.room_temperature": "—"},
    )

    assert "—".encode() in svg
    assert b"#808080" in svg


def test_default_temperature_palette_keeps_mild_weather_cool(
    tmp_path: Path,
) -> None:
    renderer = _renderer(tmp_path)
    binding = renderer.bindings[0]

    assert _renderer_module._temperature_color(-20, binding) == "#3288BD"
    assert _renderer_module._temperature_color(0, binding) == "#55AFAD"
    assert _renderer_module._temperature_color(10, binding) == "#66C2A5"
    assert _renderer_module._temperature_color(12, binding) == "#7AC6A2"
    assert _renderer_module._temperature_color(25, binding) == "#FEE08B"
    assert _renderer_module._temperature_color(32.5, binding) == "#EA8F6D"
    assert _renderer_module._temperature_color(40, binding) == "#D53E4F"
    assert _renderer_module._temperature_color(50, binding) == "#D53E4F"


def test_rejects_svg_without_entity_bindings(tmp_path: Path) -> None:
    renderer = _renderer(
        tmp_path,
        '<svg xmlns="http://www.w3.org/2000/svg">'
        '<rect id="room" width="100" height="100"/></svg>',
        assignments=(),
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
    renderer = _renderer(tmp_path, source, assignments=())

    assert renderer.assignable_elements == (
        _renderer_module.AssignableElement("room", "rect", "Living room"),
        _renderer_module.AssignableElement("hall", "rect", "Rooms"),
    )


def test_assigns_entity_to_svg_element_from_configuration(tmp_path: Path) -> None:
    source = """\
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100">
  <rect id="room" width="100" height="100" fill="white"/>
</svg>
"""
    svg_path = tmp_path / "floorplan.svg"
    svg_path.write_text(source, encoding="utf-8")
    renderer = FloorplanRenderer(
        svg_path,
        (
            ElementAssignment(
                "room",
                color_entity_id="sensor.room_temperature",
                color_mode="fill",
            ),
        ),
    )

    svg, png = renderer.render({"sensor.room_temperature": "22"})

    assert renderer.entity_ids == ("sensor.room_temperature",)
    assert b'fill: #E0DA90' in svg
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
    assert b"#E0DA90" in svg
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


def test_attic_text_renders_above_later_svg_layers(tmp_path: Path) -> None:
    source = """\
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 400 180">
  <g transform="translate(200.7317,65.802834)">
    <path id="attic"
          d="m 4.5338674,-55.084905 107.2516826,37.328033 -1.55172,6.574732 h -211.03449 l -0.84603,-7.079894 z"/>
  </g>
  <rect x="0" y="0" width="400" height="180" fill="#000000"/>
</svg>
"""
    svg_path = tmp_path / "floorplan.svg"
    svg_path.write_text(source, encoding="utf-8")
    renderer = FloorplanRenderer(
        svg_path,
        (ElementAssignment("attic", display_entity_ids=("sensor.temperature",)),),
    )

    svg, png = renderer.render(
        {"sensor.temperature": "22"},
        {"sensor.temperature": "22 °C"},
    )
    root = _renderer_module.ET.fromstring(svg)
    overlay_layer = root[-1]
    overlay = next(
        element for element in overlay_layer.iter() if element.tag.endswith("text")
    )
    assert overlay.attrib["fill"] == "#FFFFFF"
    assert overlay.attrib["stroke"] == "#17212b"
    assert [span.text for span in overlay if span.tag.endswith("tspan")] == ["22 °C"]
    image = Image.open(BytesIO(png)).convert("RGB")
    assert max(image.crop((190, 30, 220, 50)).getchannel("R").get_flattened_data()) > 100


def test_example_house_attic_renders_text_inside_transformed_path(
    tmp_path: Path,
) -> None:
    example = Path(__file__).parents[1] / "examples" / "example_house.svg"
    renderer = FloorplanRenderer(
        example,
        (ElementAssignment("rect7", display_entity_ids=("sensor.attic",)),),
    )
    svg, png = renderer.render(
        {"sensor.attic": "22"},
        {"sensor.attic": "22 °C"},
    )
    baseline = FloorplanRenderer(example).render({})[1]
    root = _renderer_module.ET.fromstring(svg)
    overlay = next(element for element in root.iter() if element.tag.endswith("text"))
    assert [span.text for span in overlay if span.tag.endswith("tspan")] == ["22 °C"]
    assert float(overlay.attrib["x"]) == pytest.approx(4.54276)
    assert float(overlay.attrib["y"]) == pytest.approx(-26.142)

    rendered_image = Image.open(BytesIO(png)).convert("RGB")
    baseline_image = Image.open(BytesIO(baseline)).convert("RGB")
    attic = (700, 100, 850, 200)
    assert ImageChops.difference(
        rendered_image.crop(attic), baseline_image.crop(attic)
    ).getbbox()


def test_fade_mode_follows_shape_edges_and_uses_neutral_underlay(
    tmp_path: Path,
) -> None:
    source = """\
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100">
  <rect id="background" width="100" height="100" fill="#ffffff"/>
  <path id="room" d="M 20 20 L 80 20 L 50 80 Z"/>
</svg>
"""
    svg_path = tmp_path / "floorplan.svg"
    svg_path.write_text(source, encoding="utf-8")
    renderer = FloorplanRenderer(
        svg_path,
        (
            ElementAssignment(
                "room",
                color_entity_id="sensor.temperature",
                color_mode="fade",
            ),
        ),
    )

    svg, png = renderer.render({"sensor.temperature": "22"})

    assert b"ha-edge-fade-room" in svg
    image = Image.open(BytesIO(png)).convert("RGB")
    center = image.getpixel((50, 40))
    near_edge = image.getpixel((50, 22))
    outside = image.getpixel((50, 18))
    assert center == (224, 218, 144)
    assert near_edge != (255, 255, 255)
    assert near_edge != center
    assert outside == (255, 255, 255)


def test_transparent_fade_reveals_background_at_shape_edges(
    tmp_path: Path,
) -> None:
    source = """\
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100">
  <rect id="background" width="100" height="100" fill="#ffffff"/>
  <path id="outskirts" d="M 20 20 L 80 20 L 50 80 Z"/>
</svg>
"""
    svg_path = tmp_path / "floorplan.svg"
    svg_path.write_text(source, encoding="utf-8")
    renderer = FloorplanRenderer(
        svg_path,
        (
            ElementAssignment(
                "outskirts",
                color_entity_id="sensor.temperature",
                color_mode="fade-transparent",
            ),
        ),
    )

    svg, png = renderer.render({"sensor.temperature": "22"})

    assert b"fill: none" in svg
    image = Image.open(BytesIO(png)).convert("RGB")
    center = image.getpixel((50, 40))
    near_edge = image.getpixel((50, 22))
    outside = image.getpixel((50, 18))
    assert center == (224, 218, 144)
    assert near_edge != (255, 255, 255)
    assert near_edge != center
    assert outside == (255, 255, 255)


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


def test_generated_text_uses_consistent_size_across_shapes(tmp_path: Path) -> None:
    source = """\
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 400 180">
  <rect id="room" x="10" y="10" width="30" height="20"/>
  <rect id="backdrop" width="400" height="180"/>
</svg>
"""
    svg_path = tmp_path / "floorplan.svg"
    svg_path.write_text(source, encoding="utf-8")
    renderer = FloorplanRenderer(
        svg_path,
        (
            ElementAssignment("room", display_entity_ids=("sensor.room",)),
            ElementAssignment("backdrop", display_entity_ids=("sensor.outside",)),
        ),
    )

    svg, _ = renderer.render(
        {"sensor.room": "22", "sensor.outside": "3.25"},
        {
            "sensor.room": "22 °C",
            "sensor.outside": "3.25 °C",
        },
    )
    root = _renderer_module.ET.fromstring(svg)
    font_sizes = [
        float(element.attrib["font-size"])
        for element in root.iter()
        if element.tag.endswith("text")
    ]

    assert len(font_sizes) == 2
    assert font_sizes[0] == pytest.approx(font_sizes[1])
    assert font_sizes[0] == pytest.approx(180 * 0.04)


def test_json_sidecar_stores_multiple_independent_configurations(
    tmp_path: Path,
) -> None:
    source = """\
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100">
  <rect id="room" width="100" height="100"/>
  <rect id="humidity" width="50" height="50"/>
</svg>
"""
    source_path = tmp_path / "floorplan.svg"
    source_path.write_text(source, encoding="utf-8")
    temperature_assignments = (
        ElementAssignment(
            "room",
            display_entity_ids=("sensor.temperature",),
            display_position="top-right",
            improve_readability=True,
            color_entity_id="sensor.temperature",
            color_mode="fade",
        ),
    )
    humidity_assignments = (
        ElementAssignment(
            "humidity",
            display_entity_ids=("sensor.humidity",),
            color_entity_id="sensor.humidity",
            color_mode="fill",
        ),
    )
    source_bytes = source_path.read_bytes()
    save_configuration(source_path, "temperature", temperature_assignments)
    save_configuration(source_path, "humidity", humidity_assignments)

    temperature = FloorplanRenderer(
        source_path, load_configuration(source_path, "temperature")
    )
    temperature_svg, _ = temperature.render(
        {"sensor.temperature": "22", "sensor.humidity": "45"},
        {"sensor.temperature": "22 °C", "sensor.humidity": "45 %"},
    )
    humidity = FloorplanRenderer(
        source_path, load_configuration(source_path, "humidity")
    )
    humidity_svg, _ = humidity.render(
        {"sensor.temperature": "22", "sensor.humidity": "45"},
        {"sensor.temperature": "22 °C", "sensor.humidity": "45 %"},
    )

    assert temperature.entity_ids == ("sensor.temperature",)
    assert temperature.assignments[0].improve_readability
    assert humidity.entity_ids == ("sensor.humidity",)
    assert b"22 \xc2\xb0C" in temperature_svg
    assert b"45 %" not in temperature_svg
    assert b"45 %" in humidity_svg
    assert b"22 \xc2\xb0C" not in humidity_svg
    assert source_path.read_bytes() == source_bytes
    assert b"data-ha-" not in source_bytes
    json_data = json.loads(source_path.with_suffix(".json").read_text("utf-8"))
    assert set(json_data["configurations"]) == {"temperature", "humidity"}
    assert json_data["configurations"]["temperature"]["assignments"][0][
        "improve_readability"
    ]


def test_loads_legacy_text_shadow_configuration(tmp_path: Path) -> None:
    source_path = tmp_path / "floorplan.svg"
    source_path.write_text(SAMPLE_SVG, encoding="utf-8")
    source_path.with_suffix(".json").write_text(
        json.dumps(
            {
                "version": 1,
                "configurations": {
                    "legacy": {
                        "assignments": [
                            {
                                "element_id": "temperature",
                                "display_entity_ids": ["sensor.room_temperature"],
                                "text_shadow": True,
                            }
                        ]
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    assignment = load_configuration(source_path, "legacy")[0]

    assert assignment.improve_readability


def test_skipped_elements_persist_in_json_configuration(
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
    save_configuration(source_path, "neutral", assignments)
    renderer = FloorplanRenderer(
        source_path, load_configuration(source_path, "neutral")
    )
    svg, _ = renderer.render({})
    root = _renderer_module.ET.fromstring(svg)
    elements = {element.attrib.get("id"): element for element in root.iter()}

    assert elements["room"].attrib["fill"] == "#808080"
    assert elements["room"].attrib["stroke"] == "black"
    assert elements["label"].attrib["fill"] == "#404040"
    assert b'data-ha-neutral="true"' not in svg


def test_device_selection_persists_per_json_assignment(tmp_path: Path) -> None:
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

    save_configuration(source_path, "devices", assignments)
    renderer = FloorplanRenderer(
        source_path, load_configuration(source_path, "devices")
    )

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
        (
            ElementAssignment(
                "room",
                color_entity_id="sensor.room_temperature",
                color_mode="fill",
            ),
        ),
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


def test_rejects_svg_above_size_limit(tmp_path: Path, monkeypatch) -> None:
    svg_path = tmp_path / "oversized.svg"
    svg_path.write_bytes(b"<svg/>")
    monkeypatch.setattr(_renderer_module, "MAX_SVG_BYTES", 5)

    with pytest.raises(FloorplanError, match="SVG exceeds the 5 byte size limit"):
        FloorplanRenderer(svg_path)


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
