"""Image entities for rendered SVG floor plans."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from homeassistant.components.image import ImageEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.util import slugify

from . import SvgFloorplanConfigEntry, update_signal
from .const import CONF_CONFIGURATION_NAME, CONF_SOURCE_SVG_PATH, CONF_SVG_PATH


async def async_setup_entry(
    hass: HomeAssistant,
    entry: SvgFloorplanConfigEntry,
    async_add_entities,
) -> None:
    """Set up SVG and PNG images."""
    async_add_entities(
        [
            FloorplanImage(hass, entry, "svg"),
            FloorplanImage(hass, entry, "png"),
        ]
    )


class FloorplanImage(ImageEntity):
    """An SVG or PNG image of a rendered floor plan."""

    def __init__(
        self, hass: HomeAssistant, entry: SvgFloorplanConfigEntry, image_format: str
    ) -> None:
        super().__init__(hass)
        self._entry = entry
        self._image_format = image_format
        source_path = entry.data.get(
            CONF_SOURCE_SVG_PATH, entry.data[CONF_SVG_PATH]
        )
        svg_name = Path(source_path).stem
        configuration_name = entry.data.get(CONF_CONFIGURATION_NAME, entry.title)
        self._attr_unique_id = (
            f"{entry.entry_id}_{configuration_name}_{image_format}"
        )
        self._attr_name = (
            f"{svg_name} {configuration_name} {image_format.upper()}"
        )
        self._attr_suggested_object_id = (
            f"floorplan_{slugify(svg_name, separator='_')}_"
            f"{slugify(configuration_name, separator='_')}_{image_format}"
        )
        self._attr_entity_registry_enabled_default = True
        self._attr_content_type = (
            "image/svg+xml" if image_format == "svg" else "image/png"
        )

    @property
    def image_last_updated(self) -> datetime:
        """Return the time the generated image last changed."""
        return self._entry.runtime_data.image_last_updated

    async def async_added_to_hass(self) -> None:
        """Subscribe to regenerated images."""
        await super().async_added_to_hass()
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                update_signal(self._entry.entry_id),
                self._handle_image_update,
            )
        )

    @callback
    def _handle_image_update(self) -> None:
        """Refresh the entity state on Home Assistant's event loop."""
        self.async_write_ha_state()

    async def async_image(self) -> bytes:
        """Return the current rendered image."""
        runtime_data = self._entry.runtime_data
        return runtime_data.svg if self._image_format == "svg" else runtime_data.png
