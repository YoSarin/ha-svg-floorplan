"""Config flow for SVG Floorplan Image."""

from __future__ import annotations

import logging
from pathlib import Path

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.core import HomeAssistant

from . import _source_path
from .const import CONF_SVG_PATH, DOMAIN
from .renderer import FloorplanError, FloorplanRenderer

_LOGGER = logging.getLogger(__name__)


async def _validate(hass: HomeAssistant, path: str) -> str | None:
    """Validate the configured SVG path and metadata."""
    try:
        source_path = _source_path(hass, path)
        renderer = await hass.async_add_executor_job(FloorplanRenderer, source_path)
        await hass.async_add_executor_job(renderer.render, {})
    except (FloorplanError, OSError) as err:
        _LOGGER.debug("Invalid SVG floor plan: %s", err)
        return "invalid_svg"
    if not renderer.entity_ids:
        return "invalid_svg"
    return None


class SvgFloorplanConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Set up an SVG Floorplan Image entry."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, str] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Ask for the SVG source file."""
        errors: dict[str, str] = {}
        if user_input is not None:
            path = user_input[CONF_SVG_PATH].strip()
            error = await _validate(self.hass, path)
            if error is None:
                await self.async_set_unique_id(path)
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title=Path(path).stem,
                    data={CONF_SVG_PATH: path},
                )
            errors["base"] = error

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_SVG_PATH): str,
                }
            ),
            errors=errors,
        )
