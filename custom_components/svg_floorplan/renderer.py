"""Read and render SVG floor plans using Home Assistant entity states."""

from __future__ import annotations

import math
import re
import xml.etree.ElementTree as ET
from copy import deepcopy
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
    DEFAULT_PURPLE_AT,
    DEFAULT_PURPLE_COLOR,
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
NEUTRAL_COLOR = "#808080"
NEUTRAL_TEXT_COLOR = "#404040"
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
TEXT_OVERLAY_TAGS = {"circle", "ellipse", "path", "polygon", "rect"}


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
    edge_fade_ids: tuple[str, ...] = ()
    transparent_fade_ids: tuple[str, ...] = ()
    purple_at: float = DEFAULT_PURPLE_AT
    purple_color: str = DEFAULT_PURPLE_COLOR


@dataclass(frozen=True)
class ElementAssignment:
    """Sensor display and color behavior configured for one SVG element."""

    element_id: str
    entity_id: str | None = None
    role: str | None = None
    display_entity_ids: tuple[str, ...] = ()
    display_position: str = "auto"
    color_entity_id: str | None = None
    color_mode: str = "none"
    neutralize: bool = False
    device_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class AssignableElement:
    """An SVG element that can be configured from the integration setup flow."""

    element_id: str
    tag: str
    label: str


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
    if value <= binding.comfort:
        amount = (value - binding.minimum) / (binding.comfort - binding.minimum)
        return _interpolate_color(binding.cold_color, binding.comfort_color, amount)
    if value <= binding.maximum:
        amount = (value - binding.comfort) / (binding.maximum - binding.comfort)
        return _interpolate_color(binding.comfort_color, binding.hot_color, amount)
    if value >= binding.purple_at:
        return binding.purple_color
    amount = (value - binding.maximum) / (binding.purple_at - binding.maximum)
    return _interpolate_color(binding.hot_color, binding.purple_color, amount)


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
        {"offset": "0%", "stop-color": color, "stop-opacity": "1"},
    )
    ET.SubElement(
        gradient,
        f"{{{SVG_NAMESPACE}}}stop",
        {"offset": "60%", "stop-color": color, "stop-opacity": "0.9"},
    )
    ET.SubElement(
        gradient,
        f"{{{SVG_NAMESPACE}}}stop",
        {"offset": "85%", "stop-color": color, "stop-opacity": "0.45"},
    )
    ET.SubElement(
        gradient,
        f"{{{SVG_NAMESPACE}}}stop",
        {"offset": "90%", "stop-color": color, "stop-opacity": "0"},
    )


def _edge_fade_width(element: ET.Element, root: ET.Element) -> float:
    """Estimate an inward edge-fade width from the element's geometry."""
    tag = element.tag.rsplit("}", 1)[-1]
    try:
        if tag == "rect":
            width = float(element.attrib["width"])
            height = float(element.attrib["height"])
        elif tag == "circle":
            width = height = float(element.attrib["r"]) * 2
        elif tag == "ellipse":
            width = float(element.attrib["rx"]) * 2
            height = float(element.attrib["ry"]) * 2
        elif tag == "polygon":
            coordinates = [
                float(value)
                for value in re.findall(
                    r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?",
                    element.attrib["points"],
                )
            ]
            width = max(coordinates[::2]) - min(coordinates[::2])
            height = max(coordinates[1::2]) - min(coordinates[1::2])
        elif tag == "path":
            points = _path_vertices(element.attrib["d"])
            width = max(point[0] for point in points) - min(
                point[0] for point in points
            )
            height = max(point[1] for point in points) - min(
                point[1] for point in points
            )
        else:
            width = height = 0
    except (KeyError, ValueError, FloorplanError):
        width = height = 0
    if width > 0 and height > 0 and math.isfinite(width + height):
        return min(width, height) * 0.2
    view_box = root.attrib.get("viewBox", "").replace(",", " ").split()
    if len(view_box) == 4:
        try:
            return min(map(float, view_box[2:])) * 0.02
        except ValueError:
            pass
    return 1.0


def _add_edge_fade_mask(
    root: ET.Element, shape: ET.Element, mask_id: str, fade_width: float
) -> None:
    """Fade the colored copy inward along its actual shape boundary."""
    defs = next(
        (child for child in root if child.tag == f"{{{SVG_NAMESPACE}}}defs"),
        None,
    )
    if defs is None:
        defs = ET.Element(f"{{{SVG_NAMESPACE}}}defs")
        root.insert(0, defs)
    mask = ET.SubElement(
        defs,
        f"{{{SVG_NAMESPACE}}}mask",
        {
            "id": mask_id,
            "maskUnits": "objectBoundingBox",
            "maskContentUnits": "userSpaceOnUse",
            "x": "-0.25",
            "y": "-0.25",
            "width": "1.5",
            "height": "1.5",
        },
    )
    base = deepcopy(shape)
    for attribute in (
        "id",
        "transform",
        "style",
        "fill",
        "stroke",
        "stroke-width",
        "stroke-opacity",
        "fill-opacity",
        "opacity",
        "mask",
        "filter",
        "clip-path",
    ):
        base.attrib.pop(attribute, None)
    base.set("fill", "white")
    base.set("stroke", "none")
    mask.append(base)
    for index in range(1, 11):
        edge = deepcopy(base)
        edge.set("fill", "none")
        edge.set("stroke", "black")
        edge.set("stroke-width", f"{fade_width * index / 10:g}")
        edge.set("stroke-opacity", "0.12")
        edge.set("stroke-linejoin", "round")
        edge.set("stroke-linecap", "round")
        mask.append(edge)


def _set_text(element: ET.Element, text: str) -> None:
    for child in list(element):
        element.remove(child)
    element.text = text


def _set_text_lines(element: ET.Element, lines: tuple[str, ...]) -> None:
    for child in list(element):
        element.remove(child)
    element.text = None
    x = element.attrib.get("x")
    for index, line in enumerate(lines):
        attributes = {"dy": "0" if index == 0 else "1.2em"}
        if x is not None:
            attributes["x"] = x
        span = ET.SubElement(element, f"{{{SVG_NAMESPACE}}}tspan", attributes)
        span.text = line


def _path_vertices(data: str) -> tuple[tuple[float, float], ...]:
    """Return vertices from a straight-line SVG path."""
    tokens = re.findall(
        r"[A-Za-z]|[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?",
        data,
    )
    points: list[tuple[float, float]] = []
    index = 0
    command: str | None = None
    x = y = start_x = start_y = 0.0
    while index < len(tokens):
        if tokens[index].isalpha():
            command = tokens[index]
            index += 1
            if command in {"Z", "z"}:
                x, y = start_x, start_y
                command = None
                continue
            if command not in {"M", "m", "L", "l", "H", "h", "V", "v"}:
                raise FloorplanError(
                    "Text placement supports straight-line SVG paths only"
                )
        if command is None:
            raise FloorplanError("Invalid SVG path for text placement")
        relative = command.islower()
        upper_command = command.upper()
        if upper_command in {"M", "L"}:
            if index + 1 >= len(tokens) or tokens[index].isalpha() or tokens[index + 1].isalpha():
                raise FloorplanError("Invalid SVG path for text placement")
            next_x, next_y = float(tokens[index]), float(tokens[index + 1])
            index += 2
            if relative:
                next_x += x
                next_y += y
            x, y = next_x, next_y
            if upper_command == "M":
                start_x, start_y = x, y
                command = "l" if relative else "L"
        elif upper_command == "H":
            if tokens[index].isalpha():
                raise FloorplanError("Invalid SVG path for text placement")
            x = float(tokens[index]) + (x if relative else 0)
            index += 1
        else:
            if tokens[index].isalpha():
                raise FloorplanError("Invalid SVG path for text placement")
            y = float(tokens[index]) + (y if relative else 0)
            index += 1
        points.append((x, y))
    if len(points) < 3:
        raise FloorplanError("SVG path needs at least three points for text placement")
    return tuple(points)


def _polygon_center(
    points: tuple[tuple[float, float], ...],
) -> tuple[float, float]:
    """Return an area centroid, falling back to the bounds center."""
    twice_area = 0.0
    centroid_x = centroid_y = 0.0
    for index, (x1, y1) in enumerate(points):
        x2, y2 = points[(index + 1) % len(points)]
        cross = x1 * y2 - x2 * y1
        twice_area += cross
        centroid_x += (x1 + x2) * cross
        centroid_y += (y1 + y2) * cross
    if abs(twice_area) > 1e-9:
        return centroid_x / (3 * twice_area), centroid_y / (3 * twice_area)
    return (
        (min(point[0] for point in points) + max(point[0] for point in points)) / 2,
        (min(point[1] for point in points) + max(point[1] for point in points)) / 2,
    )


def _polygon_width_at(
    points: tuple[tuple[float, float], ...], y: float
) -> float:
    intersections = []
    for index, (x1, y1) in enumerate(points):
        x2, y2 = points[(index + 1) % len(points)]
        if (y1 <= y < y2) or (y2 <= y < y1):
            intersections.append(x1 + (y - y1) * (x2 - x1) / (y2 - y1))
    return max(intersections) - min(intersections) if len(intersections) >= 2 else 0


def _text_width_units(text: str) -> float:
    """Estimate text width in font-size units for safe SVG scaling."""
    narrow = set("ilI.,:;!|'`")
    wide = set("MW@%&#")
    uppercase = set("ABCDEFGHKNOPQRSTUVXYZ")
    return sum(
        1.0
        if ord(character) > 0x2E80
        else 0.3
        if character in narrow
        else 0.9
        if character in wide
        else 0.68
        if character in uppercase
        else 0.56
        if character.isdigit()
        else 0.32
        if character.isspace()
        else 0.54
        for character in text
    )


def order_display_entities(
    selected: tuple[str, ...], requested_order: tuple[str, ...]
) -> tuple[str, ...]:
    """Apply explicit reading positions, then preserve order for remaining entities."""
    selected_ids = tuple(dict.fromkeys(selected))
    ordered_ids = list(
        dict.fromkeys(
            entity_id
            for entity_id in requested_order
            if entity_id in selected_ids
        )
    )
    ordered_ids.extend(
        entity_id for entity_id in selected_ids if entity_id not in ordered_ids
    )
    return tuple(ordered_ids)


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
) -> tuple[tuple[RoomBinding, ...], dict[str, tuple[str, ...]]]:
    bindings: list[RoomBinding] = []
    display_entities_by_element: dict[str, tuple[str, ...]] = {}
    assigned_elements: set[str] = set()
    elements_by_id = {
        element_id: element
        for element in root.iter()
        if (element_id := _element_id(element)) is not None
    }
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
        purple_at = _parse_number(
            group.attrib.get("data-ha-purple-at"),
            DEFAULT_PURPLE_AT,
            "data-ha-purple-at",
        )
        if not minimum < comfort < maximum < purple_at:
            raise FloorplanError(
                "Temperature color stops must satisfy min < comfort < max < purple"
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
                purple_at=purple_at,
                purple_color=_parse_color(
                    group.attrib.get("data-ha-color-purple"),
                    DEFAULT_PURPLE_COLOR,
                    "data-ha-color-purple",
                ),
            )
        )

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
        tag = element.tag.rsplit("}", 1)[-1]
        if assignment.neutralize:
            continue

        # Keep entries created by older versions working.
        if assignment.entity_id is not None and assignment.role is not None:
            if not assignment.entity_id.startswith(("sensor.", "input_number.")):
                raise FloorplanError(
                    f"{assignment.entity_id!r} must be a sensor or input_number entity"
                )
            if assignment.role not in VALUE_ROLES:
                raise FloorplanError(
                    f"{assignment.role!r} is not a supported element role"
                )
            if assignment.role == "value":
                display_entities_by_element[assignment.element_id] = (
                    assignment.entity_id,
                )
            else:
                bindings.append(
                    RoomBinding(
                        entity_id=assignment.entity_id,
                        area_ids=(assignment.element_id,)
                        if assignment.role == "area"
                        else (),
                        glow_ids=(assignment.element_id,)
                        if assignment.role == "glow"
                        else (),
                        value_ids=(),
                        minimum=DEFAULT_MIN,
                        comfort=DEFAULT_COMFORT,
                        maximum=DEFAULT_MAX,
                        cold_color=DEFAULT_COLD_COLOR,
                        comfort_color=DEFAULT_COMFORT_COLOR,
                        hot_color=DEFAULT_HOT_COLOR,
                        unit=DEFAULT_UNIT,
                    )
                )
            continue

        if assignment.display_entity_ids and tag not in {"text", *TEXT_OVERLAY_TAGS}:
            raise FloorplanError(
                f"Display sensors need a text or supported shape element "
                f"({assignment.element_id!r})"
            )
        if any(
            not entity_id.startswith(("sensor.", "input_number."))
            for entity_id in assignment.display_entity_ids
        ):
            raise FloorplanError(
                "Display entities must be sensors or input_number entities"
            )
        if assignment.display_entity_ids:
            if assignment.display_position not in {
                "auto",
                "center",
                "top-left",
                "top-right",
                "bottom-left",
                "bottom-right",
            }:
                raise FloorplanError(
                    f"Unsupported text position {assignment.display_position!r}"
                )
            display_entities_by_element[assignment.element_id] = (
                assignment.display_entity_ids
            )

        if assignment.color_mode not in {
            "none",
            "fill",
            "fade",
            "fade-transparent",
        }:
            raise FloorplanError("Color mode must be none, fill, or fade")
        if assignment.color_mode == "none":
            if assignment.color_entity_id is not None:
                raise FloorplanError(
                    "Choose fill or fade to use a color sensor"
                )
            continue
        if assignment.color_entity_id is None:
            raise FloorplanError("Choose a sensor to drive the element color")
        if not assignment.color_entity_id.startswith(("sensor.", "input_number.")):
            raise FloorplanError(
                f"{assignment.color_entity_id!r} must be a sensor or input_number entity"
            )
        bindings.append(
            RoomBinding(
                entity_id=assignment.color_entity_id,
                area_ids=(assignment.element_id,)
                if assignment.color_mode == "fill"
                else (),
                glow_ids=(),
                edge_fade_ids=(assignment.element_id,)
                if assignment.color_mode == "fade"
                else (),
                transparent_fade_ids=(assignment.element_id,)
                if assignment.color_mode == "fade-transparent"
                else (),
                value_ids=(),
                minimum=DEFAULT_MIN,
                comfort=DEFAULT_COMFORT,
                maximum=DEFAULT_MAX,
                cold_color=DEFAULT_COLD_COLOR,
                comfort_color=DEFAULT_COMFORT_COLOR,
                hot_color=DEFAULT_HOT_COLOR,
                unit=DEFAULT_UNIT,
            )
        )

    return tuple(bindings), display_entities_by_element


def _add_text_overlay(
    element: ET.Element,
    parent: ET.Element,
    lines: tuple[str, ...],
    position: str,
    root: ET.Element,
    transform_chain: tuple[str, ...],
) -> None:
    """Place sensor readings centered and scaled within an SVG shape."""
    tag = element.tag.rsplit("}", 1)[-1]
    polygon_points: tuple[tuple[float, float], ...] | None = None
    try:
        if tag == "rect":
            x = float(element.attrib.get("x", "0"))
            y = float(element.attrib.get("y", "0"))
            width = float(element.attrib["width"])
            height = float(element.attrib["height"])
        elif tag == "circle":
            radius = float(element.attrib["r"])
            x = float(element.attrib["cx"]) - radius
            y = float(element.attrib["cy"]) - radius
            width = height = radius * 2
        elif tag == "ellipse":
            rx = float(element.attrib["rx"])
            ry = float(element.attrib["ry"])
            x = float(element.attrib["cx"]) - rx
            y = float(element.attrib["cy"]) - ry
            width, height = rx * 2, ry * 2
        elif tag == "polygon":
            coordinates = [
                float(value)
                for value in re.findall(
                    r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?",
                    element.attrib["points"],
                )
            ]
            if len(coordinates) < 6 or len(coordinates) % 2:
                raise FloorplanError("Invalid SVG polygon for text placement")
            polygon_points = tuple(zip(coordinates[::2], coordinates[1::2], strict=True))
            x = min(point[0] for point in polygon_points)
            y = min(point[1] for point in polygon_points)
            width = max(point[0] for point in polygon_points) - x
            height = max(point[1] for point in polygon_points) - y
        elif tag == "path":
            polygon_points = _path_vertices(element.attrib["d"])
            x = min(point[0] for point in polygon_points)
            y = min(point[1] for point in polygon_points)
            width = max(point[0] for point in polygon_points) - x
            height = max(point[1] for point in polygon_points) - y
        else:
            raise FloorplanError(
                f"Cannot place display text inside SVG <{tag}> elements"
            )
    except (KeyError, ValueError) as err:
        raise FloorplanError(
            f"Cannot determine the bounds of SVG element "
            f"{element.attrib.get('id', '')!r} for text placement"
        ) from err

    if not all(math.isfinite(value) for value in (x, y, width, height)):
        raise FloorplanError("SVG element bounds must be finite numbers")
    if width <= 0 or height <= 0:
        raise FloorplanError("SVG element bounds must have positive size")
    center_x, center_y = (
        _polygon_center(polygon_points)
        if polygon_points
        else (x + width / 2, y + height / 2)
    )

    view_box = root.attrib.get("viewBox", "").replace(",", " ").split()
    canvas_font_size = min(width, height) * 0.14
    if len(view_box) == 4:
        try:
            canvas_width, canvas_height = map(float, view_box[2:])
        except ValueError:
            pass
        else:
            if (
                math.isfinite(canvas_width)
                and math.isfinite(canvas_height)
                and canvas_width > 0
                and canvas_height > 0
            ):
                canvas_font_size = min(canvas_width, canvas_height) * 0.04
    if position == "auto":
        if len(view_box) == 4:
            try:
                view_width, view_height = map(float, view_box[2:])
            except ValueError:
                view_width = view_height = 0
        else:
            view_width = view_height = 0
        is_canvas_sized = (
            view_width > 0
            and view_height > 0
            and width >= view_width * 0.75
            and height >= view_height * 0.75
        )
        position = "top-left" if is_canvas_sized else "center"

    inner_width = width * 0.84
    if polygon_points:
        inner_width = min(
            _polygon_width_at(polygon_points, center_y - height * 0.12),
            _polygon_width_at(polygon_points, center_y),
            _polygon_width_at(polygon_points, center_y + height * 0.12),
        ) * 0.84
        if inner_width <= 0:
            inner_width = width * 0.84
    inner_height = height * 0.84
    text_width = max(
        (_text_width_units(line) for line in lines),
        default=1.0,
    )
    font_size = min(
        canvas_font_size,
        inner_height / (1.2 * max(len(lines), 1)),
        inner_width / text_width,
    )
    text_anchor = "middle"
    baseline = "middle"
    text_x = center_x
    text_y = center_y
    if position != "center":
        margin = min(width, height) * 0.06
        if position.endswith("left"):
            text_x = x + margin
            text_anchor = "start"
        elif position.endswith("right"):
            text_x = x + width - margin
            text_anchor = "end"
        if position.startswith("top"):
            text_y = y + margin + font_size
            baseline = "auto"
        elif position.startswith("bottom"):
            text_y = y + height - margin
            baseline = "auto"
        else:
            raise FloorplanError(f"Unsupported text position {position!r}")

    text = ET.Element(
        f"{{{SVG_NAMESPACE}}}text",
        {
            "x": f"{text_x:g}",
            "y": f"{text_y:g}",
            "text-anchor": text_anchor,
            "dominant-baseline": baseline,
            "font-family": "sans-serif",
            "font-size": f"{font_size:g}",
            "fill": "#FFFFFF",
            "stroke": "#17212b",
            "stroke-width": f"{font_size * 0.12:g}",
            "stroke-linejoin": "round",
            "paint-order": "stroke",
        },
    )
    _set_text_lines(text, lines)
    max_line_width = inner_width
    for span, line in zip(
        (child for child in text if child.tag.endswith("tspan")),
        lines,
        strict=True,
    ):
        estimated_width = font_size * _text_width_units(line)
        if estimated_width > max_line_width:
            span.set("textLength", f"{max_line_width:g}")
            span.set("lengthAdjust", "spacingAndGlyphs")
    if transform := element.attrib.get("transform"):
        text.set("transform", transform)
    for transform in transform_chain:
        parent = ET.SubElement(
            parent,
            f"{{{SVG_NAMESPACE}}}g",
            {"transform": transform},
        )
    parent.append(text)


def _assignments_from_svg(root: ET.Element) -> tuple[ElementAssignment, ...]:
    assignments: list[ElementAssignment] = []
    for element in root.iter():
        element_id = _element_id(element)
        if element_id is None:
            continue
        display_entities = tuple(
            entity_id
            for entity_id in element.attrib.get(
                "data-ha-display-entities", ""
            ).split(",")
            if entity_id
        )
        color_entity = element.attrib.get("data-ha-color-entity")
        color_mode = element.attrib.get("data-ha-color-mode", "none")
        neutralize = element.attrib.get("data-ha-neutral") == "true"
        device_ids = tuple(
            device_id
            for device_id in element.attrib.get(
                "data-ha-device-ids", ""
            ).split(",")
            if device_id
        )
        legacy_device_id = element.attrib.get("data-ha-device-id")
        if not device_ids and legacy_device_id:
            device_ids = (legacy_device_id,)
        display_position = element.attrib.get(
            "data-ha-display-position", "auto"
        )
        if (
            display_entities
            or color_entity
            or color_mode != "none"
            or neutralize
            or device_ids
        ):
            assignments.append(
                ElementAssignment(
                    element_id=element_id,
                    display_entity_ids=display_entities,
                    display_position=display_position,
                    color_entity_id=color_entity,
                    color_mode=color_mode,
                    neutralize=neutralize,
                    device_ids=device_ids,
                )
            )
    return tuple(assignments)


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
        self.assignments = assignments or _assignments_from_svg(root)
        self.bindings, self.display_entities_by_element = _bindings(
            root, self.assignments
        )
        self.neutralized_elements = tuple(
            assignment.element_id
            for assignment in self.assignments
            if assignment.neutralize
        )
        self.display_position_by_element = {
            assignment.element_id: assignment.display_position
            for assignment in self.assignments
            if assignment.display_entity_ids
        }
        assignable_elements: list[AssignableElement] = []

        def find_assignable(
            element: ET.Element, inherited_label: str = ""
        ) -> None:
            label = next(
                (
                    value
                    for key, value in element.attrib.items()
                    if key.rsplit("}", 1)[-1] in {"label", "aria-label"}
                    and value.strip()
                ),
                inherited_label,
            )
            element_id = _element_id(element)
            tag = element.tag.rsplit("}", 1)[-1]
            if element_id is not None and tag in ASSIGNABLE_TAGS:
                assignable_elements.append(
                    AssignableElement(element_id, tag, label or element_id)
                )
            for child in element:
                find_assignable(child, label)

        find_assignable(root)
        self.assignable_elements = tuple(assignable_elements)

    def configured_svg(self, assignments: tuple[ElementAssignment, ...]) -> bytes:
        """Return an SVG with entity display and color choices embedded as metadata."""
        try:
            root = ET.fromstring(self._source)
        except ET.ParseError as err:
            raise FloorplanError(f"SVG is not valid XML: {err}") from err
        _bindings(root, assignments)
        elements_by_id = {
            element_id: element
            for element in root.iter()
            if (element_id := _element_id(element)) is not None
        }
        for assignment in assignments:
            element = elements_by_id[assignment.element_id]
            for attribute in (
                "data-ha-display-entities",
                "data-ha-display-position",
                "data-ha-color-entity",
                "data-ha-color-mode",
                "data-ha-neutral",
                "data-ha-device-id",
                "data-ha-device-ids",
            ):
                element.attrib.pop(attribute, None)
            if assignment.display_entity_ids:
                element.set(
                    "data-ha-display-entities",
                    ",".join(assignment.display_entity_ids),
                )
                element.set(
                    "data-ha-display-position", assignment.display_position
                )
            if assignment.color_entity_id is not None:
                element.set("data-ha-color-entity", assignment.color_entity_id)
                element.set("data-ha-color-mode", assignment.color_mode)
            if assignment.neutralize:
                element.set("data-ha-neutral", "true")
            else:
                element.attrib.pop("data-ha-neutral", None)
            if assignment.device_ids:
                element.set("data-ha-device-ids", ",".join(assignment.device_ids))
        return ET.tostring(root, encoding="utf-8", xml_declaration=True)

    @property
    def entity_ids(self) -> tuple[str, ...]:
        """Return all entities referenced by this floor plan."""
        return tuple(
            dict.fromkeys(
                [
                    *(binding.entity_id for binding in self.bindings),
                    *(
                        entity_id
                        for entity_ids in self.display_entities_by_element.values()
                        for entity_id in entity_ids
                    ),
                ]
            )
        )

    def render(
        self,
        states: dict[str, str],
        display_values: dict[str, str] | None = None,
    ) -> tuple[bytes, bytes]:
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
        parents = {child: parent for parent in root.iter() for child in parent}

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
            transparent_fade_ids = set(binding.transparent_fade_ids)
            for element_id in (
                *binding.edge_fade_ids,
                *binding.transparent_fade_ids,
            ):
                element = elements_by_id[element_id]
                parent = parents.get(element)
                if parent is None:
                    raise FloorplanError(
                        f"Could not position color fade for SVG element {element_id!r}"
                    )
                colored_shape = deepcopy(element)
                colored_shape.attrib.pop("id", None)
                colored_shape.attrib.pop("mask", None)
                _set_fill(
                    element,
                    "none"
                    if element_id in transparent_fade_ids
                    else NEUTRAL_COLOR,
                )
                _set_fill(colored_shape, color)
                mask_id = f"ha-edge-fade-{element_id}"
                suffix = 2
                while mask_id in used_ids:
                    mask_id = f"ha-edge-fade-{element_id}-{suffix}"
                    suffix += 1
                used_ids.add(mask_id)
                _add_edge_fade_mask(
                    root,
                    colored_shape,
                    mask_id,
                    _edge_fade_width(element, root),
                )
                colored_shape.set("mask", f"url(#{mask_id})")
                parent.insert(list(parent).index(element) + 1, colored_shape)
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

        for element_id in self.neutralized_elements:
            element = elements_by_id[element_id]
            if element.tag.rsplit("}", 1)[-1] == "text":
                _set_fill(element, NEUTRAL_TEXT_COLOR)
            else:
                _set_fill(element, NEUTRAL_COLOR)

        display_values = display_values or {}
        text_overlays: list[tuple[ET.Element, tuple[str, ...], str]] = []
        for element_id, entity_ids in self.display_entities_by_element.items():
            lines = tuple(
                display_values.get(entity_id, f"{entity_id}: {states.get(entity_id, '—')}")
                for entity_id in entity_ids
            )
            element = elements_by_id[element_id]
            if element.tag.rsplit("}", 1)[-1] == "text":
                _set_text_lines(element, lines)
            else:
                text_overlays.append(
                    (
                        element,
                        lines,
                        self.display_position_by_element.get(element_id, "auto"),
                    )
                )

        if text_overlays:
            overlay_layer = ET.SubElement(root, f"{{{SVG_NAMESPACE}}}g")
            for element, lines, position in text_overlays:
                transform_chain = []
                ancestor = parents.get(element)
                while ancestor is not None and ancestor is not root:
                    if transform := ancestor.attrib.get("transform"):
                        transform_chain.append(transform)
                    ancestor = parents.get(ancestor)
                _add_text_overlay(
                    element,
                    overlay_layer,
                    lines,
                    position,
                    root,
                    tuple(reversed(transform_chain)),
                )

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
