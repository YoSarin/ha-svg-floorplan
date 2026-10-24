"""SVG Floorplan Image integration."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import Event, HomeAssistant
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import async_track_state_change_event

from .const import (
    CONF_ELEMENT_BINDINGS,
    CONF_SVG_PATH,
    DOMAIN,
    MAX_SVG_BYTES,
)
from .renderer import ElementAssignment, FloorplanError, FloorplanRenderer

_LOGGER = logging.getLogger(__name__)
PLATFORMS = (Platform.IMAGE,)


@dataclass
class FloorplanRuntimeData:
    """Data shared by the entities for one floor plan."""

    renderer: FloorplanRenderer
    svg: bytes
    png: bytes
    image_last_updated: datetime
    render_lock: asyncio.Lock


SvgFloorplanConfigEntry = ConfigEntry[FloorplanRuntimeData]


def update_signal(entry_id: str) -> str:
    """Return the dispatcher signal for a config entry."""
    return f"{DOMAIN}_{entry_id}_updated"


def _source_path(hass: HomeAssistant, relative_path: str) -> Path:
    candidate = Path(relative_path)
    if (
        candidate.is_absolute()
        or ".." in candidate.parts
        or candidate.suffix.lower() != ".svg"
    ):
        raise FloorplanError(
            "SVG path must be a relative .svg path inside the config directory"
        )
    config_dir = Path(hass.config.config_dir).resolve()
    source_path = (config_dir / candidate).resolve()
    if not source_path.is_relative_to(config_dir):
        raise FloorplanError("SVG path must stay inside the config directory")
    if not source_path.is_file():
        raise FloorplanError(f"SVG file does not exist: {relative_path}")
    if source_path.stat().st_size > MAX_SVG_BYTES:
        raise FloorplanError(f"SVG exceeds the {MAX_SVG_BYTES} byte size limit")
    return source_path


def _entity_states(hass: HomeAssistant, renderer: FloorplanRenderer) -> dict[str, str]:
    return {
        entity_id: state.state if (state := hass.states.get(entity_id)) else ""
        for entity_id in renderer.entity_ids
    }


def _entity_display_values(
    hass: HomeAssistant, renderer: FloorplanRenderer
) -> dict[str, str]:
    """Format selected sensors with their friendly names and measurement units."""
    display_values: dict[str, str] = {}
    for entity_id in renderer.entity_ids:
        state = hass.states.get(entity_id)
        if state is None:
            display_values[entity_id] = f"{entity_id}: —"
            continue
        name = state.attributes.get("friendly_name", entity_id)
        unit = state.attributes.get("unit_of_measurement", "")
        value = state.state
        display_values[entity_id] = f"{name}: {value} {unit}".rstrip()
    return display_values


async def _render(
    hass: HomeAssistant, runtime_data: FloorplanRuntimeData
) -> tuple[bytes, bytes]:
    states = _entity_states(hass, runtime_data.renderer)
    display_values = _entity_display_values(hass, runtime_data.renderer)
    return await hass.async_add_executor_job(
        runtime_data.renderer.render, states, display_values
    )


async def async_setup_entry(
    hass: HomeAssistant, entry: SvgFloorplanConfigEntry
) -> bool:
    """Set up an SVG floor plan from a config entry."""
    try:
        path = _source_path(hass, entry.data[CONF_SVG_PATH])
        assignments = tuple(
            ElementAssignment(
                element_id=assignment["element_id"],
                entity_id=assignment.get("entity_id"),
                role=assignment.get("role"),
                display_entity_ids=tuple(assignment.get("display_entity_ids", ())),
                display_position=assignment.get("display_position", "auto"),
                color_entity_id=assignment.get("color_entity_id"),
                color_mode=assignment.get("color_mode", "none"),
            )
            for assignment in entry.data.get(CONF_ELEMENT_BINDINGS, [])
        )
        renderer = await hass.async_add_executor_job(
            FloorplanRenderer, path, assignments
        )
        if not renderer.entity_ids:
            raise FloorplanError(
                "SVG has no Home Assistant entity bindings. Reconfigure the floor plan."
            )
        placeholder = FloorplanRuntimeData(
            renderer, b"", b"", datetime.now(UTC), asyncio.Lock()
        )
        svg, png = await _render(hass, placeholder)
    except (FloorplanError, OSError) as err:
        _LOGGER.error("Unable to set up SVG floor plan %s: %s", entry.title, err)
        return False

    runtime_data = FloorplanRuntimeData(
        renderer, svg, png, datetime.now(UTC), asyncio.Lock()
    )
    entry.runtime_data = runtime_data

    async def handle_state_change(event: Event) -> None:
        """Regenerate the images when a referenced entity changes."""
        async with runtime_data.render_lock:
            try:
                new_svg, new_png = await _render(hass, runtime_data)
            except FloorplanError:
                _LOGGER.exception("Failed to render SVG floor plan %s", entry.title)
                return
            runtime_data.svg = new_svg
            runtime_data.png = new_png
            runtime_data.image_last_updated = datetime.now(UTC)
            async_dispatcher_send(hass, update_signal(entry.entry_id))

    entry.async_on_unload(
        async_track_state_change_event(
            hass, list(renderer.entity_ids), handle_state_change
        )
    )
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(
    hass: HomeAssistant, entry: SvgFloorplanConfigEntry
) -> bool:
    """Unload a floor plan config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
