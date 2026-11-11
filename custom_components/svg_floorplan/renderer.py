"""Read and render SVG floor plans using Home Assistant entity states."""

from __future__ import annotations

import math
import re
import struct
import xml.etree.ElementTree as ET
import zlib
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime
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
    FONT_FILES,
    MAX_SVG_BYTES,
    TIMESTAMP_POSITIONS,
    UNAVAILABLE_COLOR,
)

SVG_NAMESPACE = "http://www.w3.org/2000/svg"
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


def _png_pixels_at(
    png: bytes, points: tuple[tuple[int, int], ...]
) -> tuple[tuple[int, int, int, int], ...]:
    """Decode selected pixels from the renderer's 8-bit PNG output."""
    if not png.startswith(b"\x89PNG\r\n\x1a\n"):
        raise FloorplanError("Could not inspect rendered PNG background")
    offset = 8
    width = height = bit_depth = color_type = interlace = 0
    compressed = bytearray()
    while offset + 12 <= len(png):
        length = struct.unpack_from(">I", png, offset)[0]
        chunk_type = png[offset + 4 : offset + 8]
        start = offset + 8
        end = start + length
        if end + 4 > len(png):
            raise FloorplanError("Rendered PNG is truncated")
        if chunk_type == b"IHDR":
            width, height, bit_depth, color_type, _, _, interlace = struct.unpack(
                ">IIBBBBB", png[start:end]
            )
        elif chunk_type == b"IDAT":
            compressed.extend(png[start:end])
        elif chunk_type == b"IEND":
            break
        offset = end + 4
    channels_by_type = {2: 3, 6: 4}
    channels = channels_by_type.get(color_type)
    if (
        width <= 0
        or height <= 0
        or bit_depth != 8
        or channels is None
        or interlace != 0
    ):
        raise FloorplanError("Rendered PNG uses an unsupported pixel format")
    row_size = width * channels
    try:
        image_data = zlib.decompress(compressed)
    except zlib.error as err:
        raise FloorplanError("Could not decode rendered PNG pixels") from err
    if len(image_data) != height * (row_size + 1):
        raise FloorplanError("Rendered PNG pixel data has an invalid size")

    points_by_row: dict[int, list[tuple[int, int]]] = {}
    for index, (x, y) in enumerate(points):
        if 0 <= x < width and 0 <= y < height:
            points_by_row.setdefault(y, []).append((index, x))
    colors: list[tuple[int, int, int, int] | None] = [None] * len(points)
    previous = bytearray(row_size)
    source_offset = 0
    for y in range(height):
        filter_type = image_data[source_offset]
        source_offset += 1
        row = bytearray(image_data[source_offset : source_offset + row_size])
        source_offset += row_size
        for index in range(row_size):
            left = row[index - channels] if index >= channels else 0
            above = previous[index]
            upper_left = previous[index - channels] if index >= channels else 0
            if filter_type == 1:
                row[index] = (row[index] + left) & 0xFF
            elif filter_type == 2:
                row[index] = (row[index] + above) & 0xFF
            elif filter_type == 3:
                row[index] = (row[index] + (left + above) // 2) & 0xFF
            elif filter_type == 4:
                prediction = left + above - upper_left
                distances = (
                    abs(prediction - left),
                    abs(prediction - above),
                    abs(prediction - upper_left),
                )
                predictor = (
                    left
                    if distances[0] <= distances[1] and distances[0] <= distances[2]
                    else above
                    if distances[1] <= distances[2]
                    else upper_left
                )
                row[index] = (row[index] + predictor) & 0xFF
            elif filter_type != 0:
                raise FloorplanError("Rendered PNG uses an unsupported row filter")
        for point_index, x in points_by_row.get(y, ()):
            pixel_offset = x * channels
            red, green, blue = row[pixel_offset : pixel_offset + 3]
            alpha = row[pixel_offset + 3] if channels == 4 else 255
            colors[point_index] = red, green, blue, alpha
        previous = row
    return tuple(
        color if color is not None else (0, 0, 0, 0) for color in colors
    )


def _canvas_bounds(root: ET.Element) -> tuple[float, float, float, float]:
    view_box = root.attrib.get("viewBox", "").replace(",", " ").split()
    if len(view_box) == 4:
        try:
            bounds = tuple(map(float, view_box))
        except ValueError as err:
            raise FloorplanError("SVG viewBox must contain four numbers") from err
        if all(math.isfinite(value) for value in bounds) and bounds[2] > 0 and bounds[3] > 0:
            return bounds

    def dimension(name: str, fallback: float) -> float:
        value = root.attrib.get(name, "")
        physical = PHYSICAL_LENGTH_RE.fullmatch(value)
        if physical:
            return float(physical.group(1)) * PIXELS_PER_UNIT[physical.group(2)]
        match = re.fullmatch(r"\s*([+]?(?:\d+(?:\.\d*)?|\.\d+))(?:px)?\s*", value)
        return float(match.group(1)) if match else fallback

    width, height = dimension("width", 300), dimension("height", 150)
    if not all(math.isfinite(value) and value > 0 for value in (width, height)):
        raise FloorplanError("SVG canvas dimensions must be positive finite numbers")
    return 0.0, 0.0, width, height


def _relative_luminance(color: tuple[int, int, int, int]) -> float | None:
    red, green, blue, alpha = color
    if alpha < 64:
        return None

    def linear(channel: int) -> float:
        value = channel / 255
        return value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4

    return 0.2126 * linear(red) + 0.7152 * linear(green) + 0.0722 * linear(blue)


def _image_pixel_position(
    root: ET.Element,
    image_width: int,
    image_height: int,
    x: float,
    y: float,
) -> tuple[int, int]:
    x_min, y_min, width, height = _canvas_bounds(root)
    if "viewBox" not in root.attrib:
        return (
            round((x - x_min) * image_width / width),
            round((y - y_min) * image_height / height),
        )

    alignment = root.attrib.get(
        "preserveAspectRatio", "xMidYMid meet"
    ).replace("defer", "").split()
    if alignment and alignment[0] == "none":
        return (
            round((x - x_min) * image_width / width),
            round((y - y_min) * image_height / height),
        )
    mode = alignment[1] if len(alignment) > 1 else "meet"
    scale = (
        max(image_width / width, image_height / height)
        if mode == "slice"
        else min(image_width / width, image_height / height)
    )
    extra_x = image_width - width * scale
    extra_y = image_height - height * scale
    x_alignment = alignment[0] if alignment else "xMidYMid"
    offset_x = (
        extra_x
        if x_alignment.startswith("xMax")
        else extra_x / 2
        if x_alignment.startswith("xMid")
        else 0
    )
    offset_y = (
        extra_y
        if x_alignment.endswith("YMax")
        else extra_y / 2
        if x_alignment.endswith("YMid")
        else 0
    )
    return (
        round(offset_x + (x - x_min) * scale),
        round(offset_y + (y - y_min) * scale),
    )


def _rasterize_svg(svg: bytes) -> bytes:
    """Rasterize serialized SVG bytes to PNG."""
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
            raise FloorplanError(f"Bundled fonts are missing from {font_directory}")
        return resvg_py.svg_to_bytes(
            svg_string=raster_svg.decode("utf-8"),
            font_files=font_files,
            sans_serif_family="DejaVu Sans",
        )
    except FloorplanError:
        raise
    except Exception as err:
        raise FloorplanError(f"Could not rasterize SVG as PNG: {err}") from err


@dataclass(frozen=True)
class RoomBinding:
    """An SVG element colored from a Home Assistant entity."""

    entity_id: str
    area_ids: tuple[str, ...]
    minimum: float
    comfort: float
    maximum: float
    cold_color: str
    comfort_color: str
    hot_color: str
    edge_fade_ids: tuple[str, ...] = ()
    transparent_fade_ids: tuple[str, ...] = ()
    purple_at: float = DEFAULT_PURPLE_AT
    purple_color: str = DEFAULT_PURPLE_COLOR


@dataclass(frozen=True)
class ElementAssignment:
    """Sensor display and color behavior configured for one SVG element."""

    element_id: str
    display_entity_ids: tuple[str, ...] = ()
    display_position: str = "auto"
    improve_readability: bool = False
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


def _set_text_lines(
    element: ET.Element,
    lines: tuple[str, ...],
) -> None:
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


def _numeric_svg_value(value: str | None) -> float | None:
    if value is None:
        return None
    match = re.fullmatch(
        r"\s*([+-]?(?:\d+(?:\.\d*)?|\.\d+))(?:px)?\s*", value
    )
    return float(match.group(1)) if match else None


def _add_battery_icons(
    parent: ET.Element,
    lines: tuple[str, ...],
    warning_colors: tuple[str | None, ...],
    x: float,
    y: float,
    font_size: float,
    text_anchor: str,
    transform: str | None = None,
) -> None:
    """Draw battery warning icons as vector shapes beside text readings."""
    for index, (line, color) in enumerate(zip(lines, warning_colors)):
        if color is None:
            continue
        line_width = font_size * _text_width_units(line)
        line_x = (
            x - line_width / 2
            if text_anchor == "middle"
            else x - line_width
            if text_anchor == "end"
            else x
        )
        icon_x = line_x + line_width + font_size * 0.08
        baseline_y = y + index * font_size * 1.2
        icon_y = baseline_y - font_size * 0.82
        icon = ET.SubElement(
            parent,
            f"{{{SVG_NAMESPACE}}}g",
            {
                "class": "ha-battery-warning",
                "transform": (
                    f"translate({icon_x:g} {icon_y:g}) "
                    f"scale({font_size / 12:g})"
                ),
                "aria-label": "Battery warning",
            },
        )
        if transform:
            icon.set("transform", f"{transform} {icon.attrib['transform']}")
        ET.SubElement(
            icon,
            f"{{{SVG_NAMESPACE}}}rect",
            {
                "x": "0.5",
                "y": "2",
                "width": "9",
                "height": "8",
                "rx": "1",
                "fill": "#111111",
            },
        )
        ET.SubElement(
            icon,
            f"{{{SVG_NAMESPACE}}}rect",
            {
                "x": "9",
                "y": "4",
                "width": "2",
                "height": "4",
                "rx": "0.5",
                "fill": "#111111",
            },
        )
        ET.SubElement(
            icon,
            f"{{{SVG_NAMESPACE}}}rect",
            {
                "x": "1.5",
                "y": "3",
                "width": "7",
                "height": "6",
                "rx": "0.5",
                "fill": color,
            },
        )


def _remove_text_outline(element: ET.Element) -> None:
    """Remove hard text outlines when a soft shadow is enabled."""
    for attribute in ("stroke", "stroke-width", "stroke-opacity", "paint-order"):
        element.attrib.pop(attribute, None)
    element.set("stroke", "none")
    style_items = []
    for item in element.attrib.get("style", "").split(";"):
        property_name, separator, value = item.partition(":")
        if (
            separator
            and property_name.strip().lower()
            not in {"stroke", "stroke-width", "stroke-opacity", "paint-order"}
            and value.strip()
        ):
            style_items.append(f"{property_name.strip()}: {value.strip()}")
    style_items.append("stroke: none")
    element.set("style", "; ".join(style_items))


def _text_font_size(element: ET.Element, root: ET.Element) -> float:
    """Resolve the nearest SVG font size so the halo scales with the text."""
    ancestors: list[ET.Element] = []
    parents = {child: parent for parent in root.iter() for child in parent}
    current: ET.Element | None = element
    while current is not None:
        ancestors.append(current)
        current = parents.get(current)

    font_size = 16.0
    for ancestor in reversed(ancestors):
        declarations = dict(
            (name.strip().lower(), value.strip())
            for name, value in (
                item.split(":", 1)
                for item in ancestor.attrib.get("style", "").split(";")
                if ":" in item
            )
        )
        value = declarations.get(
            "font-size", ancestor.attrib.get("font-size", "")
        )
        match = re.fullmatch(
            r"([+]?(?:\d+(?:\.\d*)?|\.\d+))(px|pt|pc|mm|cm|in|em|rem|%)?",
            value.strip(),
        )
        if match is None:
            continue
        amount = float(match.group(1))
        unit = match.group(2)
        if unit in {"em", "%"}:
            amount *= font_size / (100 if unit == "%" else 1)
        elif unit == "rem":
            amount *= 16
        elif unit == "pt":
            amount *= 96 / 72
        elif unit == "pc":
            amount *= 16
        elif unit == "mm":
            amount *= 96 / 25.4
        elif unit == "cm":
            amount *= 96 / 2.54
        elif unit == "in":
            amount *= 96
        if math.isfinite(amount) and amount > 0:
            font_size = amount
    return font_size


def _apply_readability_halo(
    root: ET.Element,
    element: ET.Element,
    used_ids: set[str],
    halo_color: str = "#000000",
) -> None:
    """Add a strong, soft dark halo behind text and remove its hard outline."""
    element_id = _element_id(element) or "text"
    filter_id = f"ha-text-shadow-{element_id}"
    suffix = 2
    while filter_id in used_ids:
        filter_id = f"ha-text-shadow-{element_id}-{suffix}"
        suffix += 1
    used_ids.add(filter_id)

    defs = next(
        (child for child in root if child.tag == f"{{{SVG_NAMESPACE}}}defs"),
        None,
    )
    if defs is None:
        defs = ET.Element(f"{{{SVG_NAMESPACE}}}defs")
        root.insert(0, defs)
    font_size = _text_font_size(element, root)
    shadow_filter = ET.SubElement(
        defs,
        f"{{{SVG_NAMESPACE}}}filter",
        {
            "id": filter_id,
            "x": "-50%",
            "y": "-75%",
            "width": "200%",
            "height": "250%",
        },
    )
    ET.SubElement(
        shadow_filter,
        f"{{{SVG_NAMESPACE}}}feGaussianBlur",
        {
            "in": "SourceAlpha",
            "stdDeviation": f"{max(font_size * 0.4, 4.0):g}",
            "result": "wide-blur",
        },
    )
    ET.SubElement(
        shadow_filter,
        f"{{{SVG_NAMESPACE}}}feFlood",
        {"flood-color": halo_color, "flood-opacity": "0.97", "result": "wide-color"},
    )
    ET.SubElement(
        shadow_filter,
        f"{{{SVG_NAMESPACE}}}feComposite",
        {
            "in": "wide-color",
            "in2": "wide-blur",
            "operator": "in",
            "result": "wide-halo",
        },
    )
    ET.SubElement(
        shadow_filter,
        f"{{{SVG_NAMESPACE}}}feMorphology",
        {
            "in": "SourceAlpha",
            "operator": "dilate",
            "radius": f"{max(font_size * 0.14, 2.0):g}",
            "result": "expanded-alpha",
        },
    )
    ET.SubElement(
        shadow_filter,
        f"{{{SVG_NAMESPACE}}}feGaussianBlur",
        {
            "in": "expanded-alpha",
            "stdDeviation": f"{max(font_size * 0.1, 1.0):g}",
            "result": "near-blur",
        },
    )
    ET.SubElement(
        shadow_filter,
        f"{{{SVG_NAMESPACE}}}feFlood",
        {"flood-color": halo_color, "flood-opacity": "1", "result": "near-color"},
    )
    ET.SubElement(
        shadow_filter,
        f"{{{SVG_NAMESPACE}}}feComposite",
        {
            "in": "near-color",
            "in2": "near-blur",
            "operator": "in",
            "result": "near-halo",
        },
    )
    merge = ET.SubElement(shadow_filter, f"{{{SVG_NAMESPACE}}}feMerge")
    ET.SubElement(merge, f"{{{SVG_NAMESPACE}}}feMergeNode", {"in": "wide-halo"})
    ET.SubElement(merge, f"{{{SVG_NAMESPACE}}}feMergeNode", {"in": "near-halo"})
    ET.SubElement(merge, f"{{{SVG_NAMESPACE}}}feMergeNode", {"in": "SourceGraphic"})

    _remove_text_outline(element)
    element.set("filter", f"url(#{filter_id})")


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
                edge_fade_ids=(assignment.element_id,)
                if assignment.color_mode == "fade"
                else (),
                transparent_fade_ids=(assignment.element_id,)
                if assignment.color_mode == "fade-transparent"
                else (),
                minimum=DEFAULT_MIN,
                comfort=DEFAULT_COMFORT,
                maximum=DEFAULT_MAX,
                cold_color=DEFAULT_COLD_COLOR,
                comfort_color=DEFAULT_COMFORT_COLOR,
                hot_color=DEFAULT_HOT_COLOR,
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
    warning_colors: tuple[str | None, ...] = (),
) -> ET.Element:
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
    _add_battery_icons(
        parent,
        lines,
        warning_colors,
        text_x,
        text_y,
        font_size,
        text_anchor,
        text.attrib.get("transform"),
    )
    return text


def _add_snapshot_timestamp(
    root: ET.Element,
    position: str,
    generated_at: datetime,
    used_ids: set[str],
) -> tuple[ET.Element, str]:
    """Add a timestamp in a corner using foreground and halo colors sampled from its background."""
    x_min, y_min, width, height = _canvas_bounds(root)
    timestamp = generated_at.strftime("%H:%M")
    margin = min(width, height) * 0.025
    font_size = min(
        min(width, height) * 0.04,
        (width - 2 * margin) / _text_width_units(timestamp),
    )
    if font_size <= 0:
        raise FloorplanError("SVG canvas is too small for a timestamp")
    right = position.endswith("right")
    bottom = position.startswith("bottom")
    text_x = x_min + width - margin if right else x_min + margin
    text_y = y_min + height - margin if bottom else y_min + margin + font_size
    text_width = font_size * _text_width_units(timestamp)
    anchor = "end" if right else "start"

    sample_x_min = (text_x - text_width if right else text_x) - font_size * 0.1
    sample_x_max = (text_x if right else text_x + text_width) + font_size * 0.1
    sample_y_min = text_y - font_size * 0.9
    sample_y_max = text_y + font_size * 0.2
    sample_root = deepcopy(root)
    sample_root.set(
        "viewBox",
        f"{sample_x_min:g} {sample_y_min:g} "
        f"{sample_x_max - sample_x_min:g} {sample_y_max - sample_y_min:g}",
    )
    sample_root.set("width", "700px")
    sample_root.set("height", "100px")
    sample_root.set("preserveAspectRatio", "none")
    sample_png = _rasterize_svg(
        ET.tostring(sample_root, encoding="utf-8", xml_declaration=True)
    )
    image_width, image_height = struct.unpack_from(">II", sample_png, 16)
    sample_points: list[tuple[int, int]] = []
    for row in range(3):
        sample_y = text_y - font_size * 0.78 + (row + 0.5) * font_size * 0.9 / 3
        for column in range(7):
            sample_x = (
                text_x - text_width + (column + 0.5) * text_width / 7
                if right
                else text_x + (column + 0.5) * text_width / 7
            )
            sample_points.append(
                _image_pixel_position(
                    sample_root, image_width, image_height, sample_x, sample_y
                )
            )

    colors = _png_pixels_at(sample_png, tuple(sample_points))
    luminances = [
        luminance
        for color in colors
        if (luminance := _relative_luminance(color)) is not None
    ]
    background_is_dark = (
        sum(luminances) / len(luminances) < 0.45 if luminances else True
    )
    text_color, halo_color = (
        ("#FFFFFF", "#000000")
        if background_is_dark
        else ("#111111", "#FFFFFF")
    )
    text_id = "ha-snapshot-timestamp"
    suffix = 2
    while text_id in used_ids:
        text_id = f"ha-snapshot-timestamp-{suffix}"
        suffix += 1
    used_ids.add(text_id)
    text = ET.SubElement(
        root,
        f"{{{SVG_NAMESPACE}}}text",
        {
            "id": text_id,
            "x": f"{text_x:g}",
            "y": f"{text_y:g}",
            "text-anchor": anchor,
            "dominant-baseline": "auto",
            "font-family": "sans-serif",
            "font-size": f"{font_size:g}",
            "fill": text_color,
            "stroke": "none",
        },
    )
    text.text = timestamp
    return text, halo_color


class FloorplanRenderer:
    """Render SVG and PNG variants from an SVG definition and entity states."""

    def __init__(
        self,
        source_path: Path,
        assignments: tuple[ElementAssignment, ...] = (),
        timestamp_position: str | None = None,
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
        self.assignments = assignments
        if timestamp_position is not None and timestamp_position not in TIMESTAMP_POSITIONS:
            raise FloorplanError(
                f"Unsupported snapshot timestamp position {timestamp_position!r}"
            )
        self.timestamp_position = timestamp_position
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
        self.readability_halo_elements = {
            assignment.element_id
            for assignment in self.assignments
            if assignment.display_entity_ids and assignment.improve_readability
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
        generated_at: datetime | None = None,
        battery_warning_entities: dict[str, str] | None = None,
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
            else:
                color = _temperature_color(value, binding)

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

        for element_id in self.neutralized_elements:
            element = elements_by_id[element_id]
            if element.tag.rsplit("}", 1)[-1] == "text":
                _set_fill(element, NEUTRAL_TEXT_COLOR)
            else:
                _set_fill(element, NEUTRAL_COLOR)

        display_values = display_values or {}
        battery_warning_entities = battery_warning_entities or {}
        text_overlays: list[
            tuple[ET.Element, tuple[str, ...], str, bool, tuple[str | None, ...]]
        ] = []
        for element_id, entity_ids in self.display_entities_by_element.items():
            lines = tuple(
                display_values.get(entity_id, f"{entity_id}: {states.get(entity_id, '—')}")
                for entity_id in entity_ids
            )
            warning_colors = tuple(
                battery_warning_entities.get(entity_id)
                for entity_id in entity_ids
            )
            element = elements_by_id[element_id]
            if element.tag.rsplit("}", 1)[-1] == "text":
                _set_text_lines(element, lines)
                x = _numeric_svg_value(element.attrib.get("x"))
                y = _numeric_svg_value(element.attrib.get("y"))
                font_size = _numeric_svg_value(element.attrib.get("font-size"))
                if font_size is None:
                    style = element.attrib.get("style", "")
                    for declaration in style.split(";"):
                        property_name, separator, value = declaration.partition(":")
                        if separator and property_name.strip() == "font-size":
                            font_size = _numeric_svg_value(value.strip())
                            break
                if x is not None and y is not None:
                    _add_battery_icons(
                        parents.get(element, root),
                        lines,
                        warning_colors,
                        x,
                        y,
                        font_size or 16.0,
                        element.attrib.get("text-anchor", "start"),
                        element.attrib.get("transform"),
                    )
                if element_id in self.readability_halo_elements:
                    _apply_readability_halo(root, element, used_ids)
            else:
                text_overlays.append(
                    (
                        element,
                        lines,
                        self.display_position_by_element.get(element_id, "auto"),
                        element_id in self.readability_halo_elements,
                        warning_colors,
                    )
                )

        if text_overlays:
            overlay_layer = ET.SubElement(root, f"{{{SVG_NAMESPACE}}}g")
            for (
                element,
                lines,
                position,
                improve_readability,
                warning_colors,
            ) in text_overlays:
                transform_chain = []
                ancestor = parents.get(element)
                while ancestor is not None and ancestor is not root:
                    if transform := ancestor.attrib.get("transform"):
                        transform_chain.append(transform)
                    ancestor = parents.get(ancestor)
                overlay = _add_text_overlay(
                    element,
                    overlay_layer,
                    lines,
                    position,
                    root,
                    tuple(reversed(transform_chain)),
                    warning_colors,
                )
                if improve_readability:
                    _apply_readability_halo(root, overlay, used_ids)

        if self.timestamp_position is not None:
            timestamp, halo_color = _add_snapshot_timestamp(
                root,
                self.timestamp_position,
                generated_at or datetime.now().astimezone(),
                used_ids,
            )
            _apply_readability_halo(root, timestamp, used_ids, halo_color)
        svg = ET.tostring(root, encoding="utf-8", xml_declaration=True)
        png = _rasterize_svg(svg)
        return svg, png
