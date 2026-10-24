"""Constants for the SVG Floorplan Image integration."""

from typing import Final

DOMAIN: Final = "svg_floorplan"
CONF_SVG_PATH: Final = "svg_path"

DEFAULT_MIN: Final = 16.0
DEFAULT_COMFORT: Final = 22.0
DEFAULT_MAX: Final = 28.0
DEFAULT_COLD_COLOR: Final = "#3B82F6"
DEFAULT_COMFORT_COLOR: Final = "#22C55E"
DEFAULT_HOT_COLOR: Final = "#EF4444"
UNAVAILABLE_COLOR: Final = "#808080"
DEFAULT_UNIT: Final = "°C"

MAX_SVG_BYTES: Final = 2_000_000
