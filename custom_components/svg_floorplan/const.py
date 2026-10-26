"""Constants for the SVG Floorplan Image integration."""

from typing import Final

DOMAIN: Final = "svg_floorplan"
CONF_SVG_PATH: Final = "svg_path"
CONF_SOURCE_SVG_PATH: Final = "source_svg_path"
CONF_DEVICE_ID: Final = "device_id"
CONF_CONFIGURATION_NAME: Final = "configuration_name"

DEFAULT_MIN: Final = -20.0
DEFAULT_COMFORT: Final = 10.0
DEFAULT_MAX: Final = 25.0
DEFAULT_PURPLE_AT: Final = 40.0
DEFAULT_COLD_COLOR: Final = "#3288BD"
DEFAULT_COMFORT_COLOR: Final = "#66C2A5"
DEFAULT_HOT_COLOR: Final = "#FEE08B"
DEFAULT_PURPLE_COLOR: Final = "#D53E4F"
UNAVAILABLE_COLOR: Final = "#808080"

MAX_SVG_BYTES: Final = 2_000_000
FONT_FILES: Final = (
    "DejaVuSans.ttf",
    "DejaVuSans-Bold.ttf",
)
