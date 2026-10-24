"""Read and render SVG floor plans using Home Assistant entity states."""

from __future__ import annotations

import math
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path

import resvg_py

from .const import (
    DEFAULT_COLD_COLOR,
    DEFAULT_COMFORT,
    DEFAULT_COMFORT_COLOR,
    DEFAULT_HOT_COLOR,
    DEFAULT_MAX,
    DEFAULT_MIN,
    DEFAULT_UNIT,
    FONT_FILES,
    MAX_SVG_BYTES,
    UNAVAILABLE_COLOR,
)

SVG_NAMESPACE = "http://www.w3.org/2000/svg"
COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")
PHYSICAL_LENGTH_RE = re.compile(
    r"^\s*([+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?)\s*(mm|cm|in|pt|pc)\s*$"
)
PIXELS_PER_UNIT = {
    "mm": 96 / 25.4,
    "cm": 96 / 2.54,
    "in": 96.0,
    "pt": 96 / 72,
    "pc": 16.0,
}
VALUE_ROLES = {"area", "glow", "value"}
ASSIGNABLE_TAGS = {
    "circle",
    "ellipse",
    "line",
    "path",
    "polygon",
    "polyline",
    "rect",
    "text",
}


class FloorplanError(ValueError):
    """Raised when the SVG floor plan is invalid or cannot be rendered."""


@dataclass(frozen=True)
class RoomBinding:
    """The SVG elements and metadata bound to a Home Assistant entity."""

    entity_id: str
    area_ids: tuple[str, ...]
    glow_ids: tuple[str, ...]
    value_ids: tuple[str, ...]
    minimum: float
    comfort: float
    maximum: float
    cold_color: str
    comfort_color: str
    hot_color: str
    unit: str


@dataclass(frozen=True)
class ElementAssignment:
    """A Home Assistant entity and rendering role assigned to an SVG element."""

    element_id: str
    entity_id: str
    role: str


@dataclass(frozen=True)
class AssignableElement:
    """An SVG element that can be configured from the integration setup flow."""

    element_id: str
    tag: str


def _element_id(element: ET.Element) -> str | None:
    return element.attrib.get("id")


def _parse_number(value: str | None, default: float, label: str) -> float:
    if value is None:
        return default
    try:
        number = float(value)
    except ValueError as err:
        raise FloorplanError(f"{label} must be a number") from err
    if not math.isfinite(number):
        raise FloorplanError(f"{label} must be finite")
    return number


def _parse_color(value: str | None, default: str, label: str) -> str:
    color = value or default
    if not COLOR_RE.fullmatch(color):
        raise FloorplanError(f"{label} must be a six-digit hex color")
    return color


def _interpolate_color(start: str, end: str, amount: float) -> str:
    start_rgb = tuple(int(start[index : index + 2], 16) for index in (1, 3, 5))
    end_rgb = tuple(int(end[index : index + 2], 16) for index in (1, 3, 5))
    channels = tuple(
        round(start_channel + (end_channel - start_channel) * amount)
        for start_channel, end_channel in zip(start_rgb, end_rgb, strict=True)
    )
    return "#" + "".join(f"{channel:02X}" for channel in channels)


def _temperature_color(value: float, binding: RoomBinding) -> str:
    if value <= binding.minimum:
        return binding.cold_color
    if value >= binding.maximum:
        return binding.hot_color
    if value <= binding.comfort:
        amount = (value - binding.minimum) / (binding.comfort - binding.minimum)
        return _interpolate_color(binding.cold_color, binding.comfort_color, amount)
    amount = (value - binding.comfort) / (binding.maximum - binding.comfort)
    return _interpolate_color(binding.comfort_color, binding.hot_color, amount)


def _set_fill(element: ET.Element, color: str) -> None:
    element.set("fill", color)
    style_items = []
    for item in element.attrib.get("style", "").split(";"):
        property_name, separator, value = item.partition(":")
        if separator and property_name.strip().lower() != "fill" and value.strip():
            style_items.append(f"{property_name.strip()}: {value.strip()}")
    style_items.append(f"fill: {color}")
    element.set("style", "; ".join(style_items))


def _set_glow(element: ET.Element, gradient_id: str) -> None:
    element.set("fill", f"url(#{gradient_id})")
    style_items = []
    for item in element.attrib.get("style", "").split(";"):
        property_name, separator, value = item.partition(":")
        if separator and property_name.strip().lower() != "fill" and value.strip():
            style_items.append(f"{property_name.strip()}: {value.strip()}")
    style_items.append(f"fill: url(#{gradient_id})")
    element.set("style", "; ".join(style_items))


def _add_glow_gradient(
    root: ET.Element, gradient_id: str, color: str
) -> None:
    defs = next(
        (child for child in root if child.tag == f"{{{SVG_NAMESPACE}}}defs"),
        None,
    )
    if defs is None:
        defs = ET.Element(f"{{{SVG_NAMESPACE}}}defs")
        root.insert(0, defs)
    gradient = ET.SubElement(
        defs,
        f"{{{SVG_NAMESPACE}}}radialGradient",
        {
            "id": gradient_id,
            "cx": "50%",
            "cy": "50%",
            "r": "50%",
        },
    )
    ET.SubElement(
        gradient,
        f"{{{SVG_NAMESPACE}}}stop",
        {"offset": "0%", "stop-color": color, "stop-opacity": "0.8"},
    )
    ET.SubElement(
        gradient,
        f"{{{SVG_NAMESPACE}}}stop",
        {"offset": "90%", "stop-color": color, "stop-opacity": "0"},
    )


def _set_text(element: ET.Element, text: str) -> None:
    for child in list(element):
        element.remove(child)
    element.text = text


def _normalize_physical_dimensions(root: ET.Element) -> None:
    """Convert physical SVG sizes to CSS pixels for rasterizer compatibility."""
    for dimension in ("width", "height"):
        value = root.attrib.get(dimension)
        if value is None:
            continue
        match = PHYSICAL_LENGTH_RE.fullmatch(value)
        if match is None:
            continue
        number = float(match.group(1))
        pixels = number * PIXELS_PER_UNIT[match.group(2)]
        if math.isfinite(pixels) and pixels > 0:
            root.set(dimension, f"{pixels:g}px")


def _bindings(
    root: ET.Element, assignments: tuple[ElementAssignment, ...]
) -> tuple[RoomBinding, ...]:
    bindings: list[RoomBinding] = []
    for group in root.iter():
        entity_id = group.attrib.get("data-ha-entity")
        if entity_id is None:
            continue
        if not entity_id.startswith(("sensor.", "input_number.")):
            raise FloorplanError(
                f"{entity_id!r} must be a sensor or input_number entity"
            )

        area_ids: list[str] = []
        glow_ids: list[str] = []
        value_ids: list[str] = []
        for child in group.iter():
            if child is group:
                continue
            role = child.attrib.get("data-ha-role")
            element_id = _element_id(child)
            if role not in VALUE_ROLES:
                continue
            if not element_id:
                raise FloorplanError(
                    f"Elements with data-ha-role={role!r} need an id attribute"
                )
            if role == "area":
                area_ids.append(element_id)
            elif role == "glow":
                glow_ids.append(element_id)
            else:
                value_ids.append(element_id)

        if not area_ids and not glow_ids and not value_ids:
            raise FloorplanError(
                f"Group for {entity_id!r} must contain an area or value element"
            )

        minimum = _parse_number(
            group.attrib.get("data-ha-min"), DEFAULT_MIN, "data-ha-min"
        )
        comfort = _parse_number(
            group.attrib.get("data-ha-comfort"), DEFAULT_COMFORT, "data-ha-comfort"
        )
        maximum = _parse_number(
            group.attrib.get("data-ha-max"), DEFAULT_MAX, "data-ha-max"
        )
        if not minimum < comfort < maximum:
            raise FloorplanError(
                "Temperature color stops must satisfy min < comfort < max"
            )

        bindings.append(
            RoomBinding(
                entity_id=entity_id,
                area_ids=tuple(area_ids),
                glow_ids=tuple(glow_ids),
                value_ids=tuple(value_ids),
                minimum=minimum,
                comfort=comfort,
                maximum=maximum,
                cold_color=_parse_color(
                    group.attrib.get("data-ha-color-cold"),
                    DEFAULT_COLD_COLOR,
                    "data-ha-color-cold",
                ),
                comfort_color=_parse_color(
                    group.attrib.get("data-ha-color-comfort"),
                    DEFAULT_COMFORT_COLOR,
                    "data-ha-color-comfort",
                ),
                hot_color=_parse_color(
                    group.attrib.get("data-ha-color-hot"),
                    DEFAULT_HOT_COLOR,
                    "data-ha-color-hot",
                ),
                unit=group.attrib.get("data-ha-unit", DEFAULT_UNIT),
            )
        )

    elements_by_id = {
        element_id: element
        for element in root.iter()
        if (element_id := _element_id(element)) is not None
    }
    assigned_elements: set[str] = set()
    assigned_by_entity: dict[str, dict[str, list[str]]] = {}
    for assignment in assignments:
        if assignment.element_id in assigned_elements:
            raise FloorplanError(
                f"SVG element {assignment.element_id!r} has multiple assignments"
            )
        assigned_elements.add(assignment.element_id)
        element = elements_by_id.get(assignment.element_id)
        if element is None or element.tag.rsplit("}", 1)[-1] not in ASSIGNABLE_TAGS:
            raise FloorplanError(
                f"SVG element {assignment.element_id!r} cannot be assigned"
            )
        if not assignment.entity_id.startswith(("sensor.", "input_number.")):
            raise FloorplanError(
                f"{assignment.entity_id!r} must be a sensor or input_number entity"
            )
        if assignment.role not in VALUE_ROLES:
            raise FloorplanError(f"{assignment.role!r} is not a supported element role")
        roles = assigned_by_entity.setdefault(
            assignment.entity_id, {"area": [], "glow": [], "value": []}
        )
        roles[assignment.role].append(assignment.element_id)

    for entity_id, roles in assigned_by_entity.items():
        bindings.append(
            RoomBinding(
                entity_id=entity_id,
                area_ids=tuple(roles["area"]),
                glow_ids=tuple(roles["glow"]),
                value_ids=tuple(roles["value"]),
                minimum=DEFAULT_MIN,
                comfort=DEFAULT_COMFORT,
                maximum=DEFAULT_MAX,
                cold_color=DEFAULT_COLD_COLOR,
                comfort_color=DEFAULT_COMFORT_COLOR,
                hot_color=DEFAULT_HOT_COLOR,
                unit=DEFAULT_UNIT,
            )
        )
    return tuple(bindings)


class FloorplanRenderer:
    """Render SVG and PNG variants from an SVG definition and entity states."""

    def __init__(
        self,
        source_path: Path,
        assignments: tuple[ElementAssignment, ...] = (),
    ) -> None:
        self.source_path = source_path
        try:
            source = source_path.read_bytes()
        except OSError as err:
            raise FloorplanError(f"Cannot read SVG file: {err}") from err
        if len(source) > MAX_SVG_BYTES:
            raise FloorplanError(
                f"SVG exceeds the {MAX_SVG_BYTES} byte size limit"
            )
        try:
            root = ET.fromstring(source)
        except ET.ParseError as err:
            raise FloorplanError(f"SVG is not valid XML: {err}") from err
        if root.tag not in (f"{{{SVG_NAMESPACE}}}svg", "svg"):
            raise FloorplanError("The root XML element must be <svg>")

        ids = [
            element_id
            for element in root.iter()
            if (element_id := _element_id(element)) is not None
        ]
        if len(ids) != len(set(ids)):
            raise FloorplanError("SVG element IDs must be unique")

        self._source = source
        self.bindings = _bindings(root, assignments)
        self.assignable_elements = tuple(
            AssignableElement(element_id, element.tag.rsplit("}", 1)[-1])
            for element in root.iter()
            if (element_id := _element_id(element)) is not None
            and element.tag.rsplit("}", 1)[-1] in ASSIGNABLE_TAGS
        )

    @property
    def entity_ids(self) -> tuple[str, ...]:
        """Return all entities referenced by this floor plan."""
        return tuple(dict.fromkeys(binding.entity_id for binding in self.bindings))

    def render(self, states: dict[str, str]) -> tuple[bytes, bytes]:
        """Return the updated SVG and its transparent PNG rendering."""
        try:
            root = ET.fromstring(self._source)
        except ET.ParseError as err:
            raise FloorplanError(f"SVG is not valid XML: {err}") from err

        elements_by_id = {
            element_id: element
            for element in root.iter()
            if (element_id := _element_id(element)) is not None
        }
        used_ids = set(elements_by_id)

        for binding in self.bindings:
            raw_state = states.get(binding.entity_id, "")
            try:
                value = float(raw_state)
            except (TypeError, ValueError):
                value = math.nan
            if not math.isfinite(value):
                color = UNAVAILABLE_COLOR
                display = "—"
            else:
                color = _temperature_color(value, binding)
                display = f"{value:g} {binding.unit}".strip()

            for element_id in binding.area_ids:
                _set_fill(elements_by_id[element_id], color)
            for element_id in binding.glow_ids:
                gradient_id = f"ha-temperature-glow-{element_id}"
                suffix = 2
                while gradient_id in used_ids:
                    gradient_id = f"ha-temperature-glow-{element_id}-{suffix}"
                    suffix += 1
                used_ids.add(gradient_id)
                _add_glow_gradient(root, gradient_id, color)
                _set_glow(elements_by_id[element_id], gradient_id)
            for element_id in binding.value_ids:
                _set_text(elements_by_id[element_id], display)

        svg = ET.tostring(root, encoding="utf-8", xml_declaration=True)
        try:
            raster_root = ET.fromstring(svg)
            _normalize_physical_dimensions(raster_root)
            raster_svg = ET.tostring(
                raster_root, encoding="utf-8", xml_declaration=True
            )
            font_directory = Path(__file__).with_name("fonts")
            font_files = [
                str(font_directory / font_file)
                for font_file in FONT_FILES
                if (font_directory / font_file).is_file()
            ]
            if len(font_files) != len(FONT_FILES):
                raise FloorplanError(
                    f"Bundled fonts are missing from {font_directory}"
                )
            png = resvg_py.svg_to_bytes(
                svg_string=raster_svg.decode("utf-8"),
                font_files=font_files,
                sans_serif_family="DejaVu Sans",
            )
        except FloorplanError:
            raise
        except Exception as err:
            raise FloorplanError(f"Could not rasterize SVG as PNG: {err}") from err
        return svg, png
