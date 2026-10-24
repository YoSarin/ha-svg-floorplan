"""Constants for the SVG Floorplan Image integration."""

from typing import Final

DOMAIN: Final = "svg_floorplan"
CONF_SVG_PATH: Final = "svg_path"
CONF_SOURCE_SVG_PATH: Final = "source_svg_path"
CONF_DEVICE_ID: Final = "device_id"
CONF_ELEMENT_BINDINGS: Final = "element_bindings"

DEFAULT_MIN: Final = 16.0
DEFAULT_COMFORT: Final = 22.0
DEFAULT_MAX: Final = 28.0
DEFAULT_COLD_COLOR: Final = "#3B82F6"
DEFAULT_COMFORT_COLOR: Final = "#22C55E"
DEFAULT_HOT_COLOR: Final = "#EF4444"
UNAVAILABLE_COLOR: Final = "#808080"
DEFAULT_UNIT: Final = "°C"

MAX_SVG_BYTES: Final = 2_000_000
FONT_FILES: Final = (
    "DejaVuSans.ttf",
    "DejaVuSans-Bold.ttf",
)
