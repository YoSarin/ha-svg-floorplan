"""SVG Floorplan Image integration."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import partial
from pathlib import Path

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import Event, HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.helpers.recorder import DATA_INSTANCE
from homeassistant.util import dt as dt_util

from .battery import BATTERY_TREND_WINDOW, battery_needs_warning
from .configuration import load_configuration, load_timestamp_position
from .const import (
    CONF_CONFIGURATION_NAME,
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
    battery_sensors_by_temperature_entity: dict[str, tuple[str, ...]]
    battery_history_available: bool


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
    """Format selected entity states with their measurement units."""
    display_values: dict[str, str] = {}
    for entity_id in renderer.entity_ids:
        state = hass.states.get(entity_id)
        if state is None:
            display_values[entity_id] = "—"
            continue
        unit = state.attributes.get("unit_of_measurement") or ""
        value = state.state
        display_values[entity_id] = f"{value} {unit}".strip()
    return display_values


def _battery_sensors_by_temperature_entity(
    hass: HomeAssistant, assignments: tuple[ElementAssignment, ...]
) -> dict[str, tuple[str, ...]]:
    """Find battery percentage sensors on the same device as each temperature sensor."""
    entity_registry = er.async_get(hass)
    battery_sensors_by_device: dict[str, tuple[str, ...]] = {}
    temperatures_by_device: dict[str, list[str]] = {}

    for assignment in assignments:
        for entity_id in assignment.display_entity_ids:
            state = hass.states.get(entity_id)
            if (
                state is None
                or state.attributes.get("device_class") != "temperature"
            ):
                continue
            registry_entry = entity_registry.async_get(entity_id)
            if registry_entry is not None and registry_entry.device_id is not None:
                temperatures_by_device.setdefault(
                    registry_entry.device_id, []
                ).append(entity_id)

    result: dict[str, tuple[str, ...]] = {}
    for device_id, temperature_entities in temperatures_by_device.items():
        if device_id not in battery_sensors_by_device:
            battery_sensors = []
            for entry in er.async_entries_for_device(entity_registry, device_id):
                if entry.domain != "sensor":
                    continue
                state = hass.states.get(entry.entity_id)
                device_class = (
                    state.attributes.get("device_class")
                    if state is not None
                    else None
                ) or getattr(entry, "original_device_class", None) or getattr(
                    entry, "device_class", None
                )
                if device_class == "battery":
                    battery_sensors.append(entry.entity_id)
            battery_sensors_by_device[device_id] = tuple(battery_sensors)
        battery_entities = battery_sensors_by_device[device_id]
        for temperature_entity in temperature_entities:
            result[temperature_entity] = battery_entities
    return result


async def _battery_warning_entities(
    hass: HomeAssistant,
    runtime_data: FloorplanRuntimeData,
    now: datetime,
) -> set[str]:
    """Return temperature entities whose associated batteries need a warning."""
    battery_ids = tuple(
        dict.fromkeys(
            battery_id
            for battery_entities in (
                runtime_data.battery_sensors_by_temperature_entity.values()
            )
            for battery_id in battery_entities
        )
    )
    if not battery_ids:
        return set()

    history_by_entity = {}
    if runtime_data.battery_history_available:
        from homeassistant.components.recorder.history import get_significant_states

        history_by_entity = await hass.async_add_executor_job(
            partial(
                get_significant_states,
                hass,
                now - BATTERY_TREND_WINDOW,
                now,
                entity_ids=list(battery_ids),
                include_start_time_state=True,
                significant_changes_only=False,
                no_attributes=True,
            )
        )

    battery_warnings = set()
    for battery_id in battery_ids:
        state = hass.states.get(battery_id)
        current_state = state.state if state is not None else None
        historical_states: Iterable[tuple[datetime, str]] = (
            (item.last_updated, item.state)
            for item in history_by_entity.get(battery_id, ())
        )
        if battery_needs_warning(current_state, historical_states, now):
            battery_warnings.add(battery_id)

    return {
        temperature_entity
        for temperature_entity, battery_entities
        in runtime_data.battery_sensors_by_temperature_entity.items()
        if battery_warnings.intersection(battery_entities)
    }


async def _render(
    hass: HomeAssistant, runtime_data: FloorplanRuntimeData
) -> tuple[bytes, bytes]:
    states = _entity_states(hass, runtime_data.renderer)
    display_values = _entity_display_values(hass, runtime_data.renderer)
    now = dt_util.now()
    battery_warnings = await _battery_warning_entities(
        hass, runtime_data, now
    )
    return await hass.async_add_executor_job(
        runtime_data.renderer.render,
        states,
        display_values,
        now,
        battery_warnings,
    )


async def async_setup_entry(
    hass: HomeAssistant, entry: SvgFloorplanConfigEntry
) -> bool:
    """Set up an SVG floor plan from a config entry."""
    try:
        configuration_name = entry.data.get(CONF_CONFIGURATION_NAME)
        if configuration_name is None:
            raise FloorplanError(
                "This floor plan entry predates JSON configurations. "
                "Remove it and add the SVG again."
            )
        path = _source_path(hass, entry.data[CONF_SVG_PATH])
        assignments = await hass.async_add_executor_job(
            load_configuration,
            path,
            configuration_name,
        )
        timestamp_position = await hass.async_add_executor_job(
            load_timestamp_position,
            path,
            configuration_name,
        )
        battery_sensors = _battery_sensors_by_temperature_entity(
            hass, assignments
        )
        battery_history_available = hass.data.get(DATA_INSTANCE) is not None
        if battery_sensors and not battery_history_available:
            _LOGGER.warning(
                "Recorder is unavailable; monthly battery decline warnings "
                "are disabled for %s",
                entry.title,
            )
        renderer = await hass.async_add_executor_job(
            FloorplanRenderer, path, assignments, timestamp_position
        )
        if not renderer.entity_ids:
            raise FloorplanError(
                "SVG has no Home Assistant entity bindings. Reconfigure the floor plan."
            )
        placeholder = FloorplanRuntimeData(
            renderer,
            b"",
            b"",
            datetime.now(UTC),
            asyncio.Lock(),
            battery_sensors,
            battery_history_available,
        )
        svg, png = await _render(hass, placeholder)
    except (FloorplanError, OSError) as err:
        _LOGGER.error("Unable to set up SVG floor plan %s: %s", entry.title, err)
        return False

    runtime_data = FloorplanRuntimeData(
        renderer,
        svg,
        png,
        datetime.now(UTC),
        asyncio.Lock(),
        battery_sensors,
        battery_history_available,
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
            hass,
            list(
                dict.fromkeys(
                    (
                        *renderer.entity_ids,
                        *(
                            battery_id
                            for battery_entities in battery_sensors.values()
                            for battery_id in battery_entities
                        ),
                    )
                )
            ),
            handle_state_change,
        )
    )
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(
    hass: HomeAssistant, entry: SvgFloorplanConfigEntry
) -> bool:
    """Unload a floor plan config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
