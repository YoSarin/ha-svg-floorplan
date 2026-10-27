"""Config flow for SVG Floorplan Image."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import selector
from homeassistant.util import slugify

from . import _source_path
from .configuration import load_configuration, save_configuration
from .const import (
    CONF_CONFIGURATION_NAME,
    CONF_DEVICE_ID,
    CONF_SOURCE_SVG_PATH,
    CONF_SVG_PATH,
    DOMAIN,
)
from .renderer import (
    ElementAssignment,
    FloorplanError,
    FloorplanRenderer,
)

_LOGGER = logging.getLogger(__name__)
_COLOR_MODES = [
    {"label": "Do not color this element", "value": "none"},
    {"label": "Solid color", "value": "fill"},
    {"label": "Fade at edges to neutral gray", "value": "fade"},
    {"label": "Fade at edges to transparent", "value": "fade-transparent"},
]
_DISPLAY_POSITIONS = [
    {"label": "Automatic (center rooms, edge of floor-plan backdrop)", "value": "auto"},
    {"label": "Center", "value": "center"},
    {"label": "Top left", "value": "top-left"},
    {"label": "Top right", "value": "top-right"},
    {"label": "Bottom left", "value": "bottom-left"},
    {"label": "Bottom right", "value": "bottom-right"},
]


def _error_key(error: FloorplanError) -> str:
    """Map validation failures to actionable config-flow messages."""
    message = str(error)
    if message.startswith("SVG path must"):
        return "invalid_path"
    if message.startswith("SVG file does not exist"):
        return "file_not_found"
    if message.startswith("SVG exceeds"):
        return "file_too_large"
    if message.startswith("Configured SVG exceeds"):
        return "file_too_large"
    if message.startswith("SVG is not valid XML"):
        return "malformed_xml"
    if message.startswith("The root XML element"):
        return "not_svg"
    if "configuration JSON" in message or "Configuration " in message:
        return "invalid_configuration_json"
    if message.startswith("Could not rasterize"):
        return "render_failed"
    if "must be a sensor or input_number entity" in message:
        return "unsupported_entity"
    if "need an id attribute" in message:
        return "missing_role_id"
    if "must contain an area or value element" in message:
        return "missing_role"
    if "Temperature color stops" in message:
        return "invalid_temperature_range"
    if "must be a six-digit hex color" in message:
        return "invalid_color"
    if "IDs must be unique" in message:
        return "duplicate_ids"
    if "Display sensors need a text or supported shape" in message:
        return "display_sensors_need_supported_element"
    if "Display entities must be sensors" in message:
        return "unsupported_display_entity"
    if "Color mode must be" in message:
        return "invalid_color_mode"
    if "Unsupported text position" in message:
        return "invalid_display_position"
    if "Choose fill or fade" in message:
        return "color_mode_required"
    if "Choose a sensor to drive" in message:
        return "color_sensor_required"
    return "invalid_metadata"


class SvgFloorplanConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Set up an SVG Floorplan Image entry."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, str] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Ask for the SVG source file."""
        errors: dict[str, str] = {}
        details = ""
        if user_input is not None:
            path = user_input[CONF_SVG_PATH].strip().replace("\\", "/")
            configuration_name = user_input[CONF_CONFIGURATION_NAME].strip()
            if not configuration_name or not slugify(
                configuration_name, separator="_"
            ):
                errors["base"] = "invalid_configuration_name"
                return self.async_show_form(
                    step_id="user",
                    data_schema=vol.Schema(
                        {
                            vol.Required(CONF_SVG_PATH): str,
                            vol.Required(CONF_CONFIGURATION_NAME): str,
                        }
                    ),
                    errors=errors,
                    description_placeholders={"details": details},
                )
            self._configuration_name = configuration_name
            try:
                await self.async_set_unique_id(
                    f"{path}:{slugify(configuration_name, separator='_')}"
                )
                self._abort_if_unique_id_configured()
                source_path = _source_path(self.hass, path)
                renderer = await self.hass.async_add_executor_job(
                    FloorplanRenderer, source_path
                )
                if renderer.entity_ids:
                    await self.hass.async_add_executor_job(renderer.render, {})
                    self._pending_assignments = []
                    return await self._create_entry(path, source_path=path)
            except (FloorplanError, OSError) as err:
                _LOGGER.debug("Invalid SVG floor plan %s: %s", path, err)
                errors["base"] = (
                    _error_key(err)
                    if isinstance(err, FloorplanError)
                    else "file_read_error"
                )
                if isinstance(err, FloorplanError):
                    details = str(err)
            else:
                if not renderer.assignable_elements:
                    errors["base"] = "no_assignable_elements"
                else:
                    self._pending_path = path
                    self._pending_elements = renderer.assignable_elements
                    self._pending_assignments: list[dict[str, Any]] = []
                    self._existing_assignments: dict[str, ElementAssignment] = {}
                    self._assignment_index = 0
                    self._legacy_device_id = None
                    return await self.async_step_device()

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_SVG_PATH): str,
                    vol.Required(CONF_CONFIGURATION_NAME): str,
                }
            ),
            errors=errors,
            description_placeholders={"details": details},
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Reconfigure assignments for an existing floor plan."""
        del user_input
        entry = self._get_reconfigure_entry()
        self._reconfigure_entry = entry
        source_path = entry.data.get(
            CONF_SOURCE_SVG_PATH, entry.data[CONF_SVG_PATH]
        )
        self._configuration_name = entry.data.get(
            CONF_CONFIGURATION_NAME, "default"
        )
        try:
            source = _source_path(self.hass, source_path)
            renderer = await self.hass.async_add_executor_job(
                FloorplanRenderer, source
            )
            existing_assignments: tuple[ElementAssignment, ...] = ()
            if CONF_CONFIGURATION_NAME in entry.data:
                existing_assignments = await self.hass.async_add_executor_job(
                    load_configuration, source, self._configuration_name
                )
            if not renderer.assignable_elements:
                return self.async_abort(
                    reason="reconfigure_error",
                    description_placeholders={
                        "details": "The SVG has no supported, identified shapes or text elements to configure."
                    },
                )
        except (FloorplanError, OSError) as err:
            _LOGGER.error("Unable to reconfigure SVG floor plan %s: %s", entry.title, err)
            return self.async_abort(
                reason="reconfigure_error",
                description_placeholders={"details": str(err)},
            )

        self._pending_path = source_path
        self._pending_elements = renderer.assignable_elements
        self._pending_assignments = []
        self._existing_assignments = {
            assignment.element_id: assignment
            for assignment in existing_assignments
        }
        self._assignment_index = 0
        self._legacy_device_id = entry.data.get(CONF_DEVICE_ID)
        return await self.async_step_device()

    def _infer_device_ids(
        self, assignment: ElementAssignment | None
    ) -> tuple[str, ...]:
        """Infer devices for an assignment from its selected entities."""
        if assignment is None:
            return ()
        entity_registry = er.async_get(self.hass)
        device_ids = list(assignment.device_ids)
        entity_ids = (
            *assignment.display_entity_ids,
            *((assignment.color_entity_id,) if assignment.color_entity_id else ()),
        )
        for entity_id in entity_ids:
            if (
                registry_entry := entity_registry.async_get(entity_id)
            ) and registry_entry.device_id and registry_entry.device_id not in device_ids:
                device_ids.append(registry_entry.device_id)
        return tuple(device_ids)

    async def async_step_device(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Choose devices whose entities are available for this element."""
        errors: dict[str, str] = {}
        if user_input is not None:
            device_ids = tuple(user_input.get("device_ids", ()))
            if not device_ids:
                self._selected_device_ids = ()
                self._device_entities = []
                return await self.async_step_assign({"skip": True})

            entity_registry = er.async_get(self.hass)
            entity_options: dict[str, dict[str, str]] = {}
            for device_id in device_ids:
                entries = er.async_entries_for_device(entity_registry, device_id)
                for entry in entries:
                    if entry.entity_id.split(".", 1)[0] not in {
                        "sensor",
                        "input_number",
                    }:
                        continue
                    if entry.disabled_by is not None:
                        continue
                    state = self.hass.states.get(entry.entity_id)
                    name = (
                        state.attributes.get("friendly_name")
                        if state is not None
                        else None
                    ) or entry.name or entry.original_name or entry.entity_id
                    entity_options[entry.entity_id] = {
                        "label": f"{name} ({entry.entity_id})",
                        "value": entry.entity_id,
                    }
            self._device_entities = list(entity_options.values())
            if not self._device_entities:
                errors["base"] = "device_has_no_sensors"
            else:
                self._selected_device_ids = device_ids
                return await self.async_step_assign()

        element = self._pending_elements[self._assignment_index]
        self.context["title_placeholders"] = {
            "element_name": element.label,
            "position": (
                f"{self._assignment_index + 1} of {len(self._pending_elements)}"
            ),
        }
        previous = self._existing_assignments.get(element.element_id)
        device_default = self._infer_device_ids(previous)
        if not device_default and self._legacy_device_id:
            device_default = (self._legacy_device_id,)
        device_field = vol.Optional("device_ids")
        if device_default:
            device_field = vol.Optional("device_ids", default=list(device_default))
        return self.async_show_form(
            step_id="device",
            data_schema=vol.Schema(
                {
                    device_field: selector.DeviceSelector(
                        selector.DeviceSelectorConfig(
                            multiple=True,
                            entity=[
                                {"domain": ["sensor", "input_number"]},
                            ]
                        )
                    ),
                }
            ),
            errors=errors,
            description_placeholders={
                "element_name": element.label,
                "element_id": element.element_id,
                "position": (
                    f"{self._assignment_index + 1} of "
                    f"{len(self._pending_elements)}"
                ),
            },
        )

    async def async_step_assign(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Choose displayed entities and color behavior for one SVG element."""
        if user_input is not None:
            element = self._pending_elements[self._assignment_index]
            display_entity_ids = tuple(user_input.get("display_entities", ()))
            color_entity_id = user_input.get("color_entity")
            color_mode = user_input.get("color_mode", "fill")
            skipped = user_input.get("skip", False)
            if skipped:
                self._pending_assignments.append(
                    {
                        "element_id": element.element_id,
                        "display_entity_ids": [],
                        "display_position": "auto",
                        "improve_readability": False,
                        "color_entity_id": None,
                        "color_mode": "none",
                        "neutralize": True,
                        "device_ids": list(self._selected_device_ids),
                    }
                )
            else:
                if color_mode != "none" and not color_entity_id:
                    return self._show_assignment_form("color_sensor_required")
                if color_mode == "none" and color_entity_id:
                    return self._show_assignment_form("color_mode_required")
                if (
                    not display_entity_ids
                    and color_mode == "none"
                    and not color_entity_id
                ):
                    return self._show_assignment_form("configure_or_skip_required")
                self._pending_assignments.append(
                    {
                        "element_id": element.element_id,
                        "display_entity_ids": list(display_entity_ids),
                        "display_position": user_input.get(
                            "display_position", "auto"
                        ),
                        "improve_readability": user_input.get(
                            "improve_readability", False
                        ),
                        "color_entity_id": color_entity_id,
                        "color_mode": color_mode,
                        "device_ids": list(self._selected_device_ids),
                    }
                )
            self._assignment_index += 1

            if self._assignment_index >= len(self._pending_elements):
                if not any(
                    not assignment.get("neutralize", False)
                    for assignment in self._pending_assignments
                ):
                    self._assignment_index = 0
                    self._pending_assignments.clear()
                    return self._show_assignment_form("no_entity_assigned")

                try:
                    source_path = _source_path(self.hass, self._pending_path)
                    assignments = self._build_assignments()
                    renderer = await self.hass.async_add_executor_job(
                        FloorplanRenderer,
                        source_path,
                        assignments,
                    )
                    await self.hass.async_add_executor_job(renderer.render, {})
                    await self.hass.async_add_executor_job(
                        save_configuration,
                        source_path,
                        self._configuration_name,
                        assignments,
                    )
                    _LOGGER.info(
                        "Saved SVG floor plan configuration %s for %s",
                        self._configuration_name,
                        source_path,
                    )
                except (FloorplanError, OSError) as err:
                    _LOGGER.error(
                        "Unable to configure SVG floor plan %s: %s",
                        self._pending_path,
                        err,
                    )
                    self._assignment_index = len(self._pending_elements) - 1
                    last_element_id = self._pending_elements[
                        self._assignment_index
                    ].element_id
                    self._pending_assignments = [
                        assignment
                        for assignment in self._pending_assignments
                        if assignment["element_id"] != last_element_id
                    ]
                    return self._show_assignment_form(
                        _error_key(err)
                        if isinstance(err, FloorplanError)
                        else "file_read_error",
                        str(err) if isinstance(err, FloorplanError) else "",
                    )
                if hasattr(self, "_reconfigure_entry"):
                    return await self._update_reconfigured_entry(source_path)
                return await self._create_entry(
                    source_path.relative_to(
                        Path(self.hass.config.config_dir).resolve()
                    ).as_posix(),
                    title=(
                        f"{Path(self._pending_path).stem} "
                        f"{self._configuration_name}"
                    ),
                    source_path=self._pending_path,
                )

            return await self.async_step_device()

        return self._show_assignment_form()

    def _show_assignment_form(
        self, error: str | None = None, details: str = ""
    ) -> config_entries.ConfigFlowResult:
        element = self._pending_elements[self._assignment_index]
        errors = {"base": error} if error else {}
        position = f"{self._assignment_index + 1} of {len(self._pending_elements)}"
        self.context["title_placeholders"] = {
            "position": position,
            "element_name": element.label,
        }
        fields: dict[Any, Any] = {}
        entity_options = self._device_entities
        available_entity_ids = {option["value"] for option in entity_options}
        previous = self._existing_assignments.get(element.element_id)
        if previous and previous.display_entity_ids:
            display_entity_defaults = [
                entity_id
                for entity_id in previous.display_entity_ids
                if entity_id in available_entity_ids
            ]
        elif previous and element.tag not in {"path", "polygon"}:
            display_entity_defaults = []
        else:
            display_entity_defaults = [
                option["value"] for option in entity_options
            ]
        temperature_entity_default = next(
            (
                option["value"]
                for option in entity_options
                if (state := self.hass.states.get(option["value"]))
                and state.attributes.get("device_class") == "temperature"
            ),
            None,
        )
        color_entity_default = (
            previous.color_entity_id
            if previous and previous.color_entity_id in available_entity_ids
            else None
            if previous and previous.color_mode == "none"
            else temperature_entity_default
        )
        if element.tag in {"text", "rect", "circle", "ellipse", "path", "polygon"}:
            fields[
                vol.Optional(
                    "display_entities",
                    default=display_entity_defaults,
                )
            ] = selector.EntitySelector(
                selector.EntitySelectorConfig(
                    include_entities=sorted(available_entity_ids),
                    multiple=True,
                    reorder=True,
                )
            )
            fields[
                vol.Optional(
                    "improve_readability",
                    default=previous.improve_readability if previous else False,
                )
            ] = bool
            if element.tag not in {"text", "path", "polygon"}:
                fields[
                    vol.Optional(
                        "display_position",
                        default=previous.display_position if previous else "auto",
                    )
                ] = (
                    selector.SelectSelector(
                        selector.SelectSelectorConfig(
                            options=_DISPLAY_POSITIONS,
                            mode=selector.SelectSelectorMode.DROPDOWN,
                        )
                    )
                )
        fields.update({
            vol.Optional(
                "color_entity",
                default=color_entity_default,
            ): selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=entity_options,
                    mode=selector.SelectSelectorMode.DROPDOWN,
                )
            ),
            vol.Optional(
                "color_mode",
                default=previous.color_mode if previous else "fill",
            ): selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=_COLOR_MODES,
                    mode=selector.SelectSelectorMode.DROPDOWN,
                )
            ),
            vol.Optional(
                "skip", default=previous.neutralize if previous else False
            ): bool,
        })
        return self.async_show_form(
            step_id="assign",
            data_schema=vol.Schema(fields),
            errors=errors,
            description_placeholders={
                "element_id": element.element_id,
                "tag": element.tag,
                "element_name": element.label,
                "details": details,
                "position": position,
            },
            last_step=self._assignment_index + 1 >= len(self._pending_elements),
        )

    def _build_assignments(self) -> tuple[ElementAssignment, ...]:
        return tuple(
            ElementAssignment(
                element_id=assignment["element_id"],
                display_entity_ids=tuple(assignment["display_entity_ids"]),
                display_position=assignment["display_position"],
                improve_readability=assignment.get(
                    "improve_readability", False
                ),
                color_entity_id=assignment["color_entity_id"],
                color_mode=assignment["color_mode"],
                neutralize=assignment.get("neutralize", False),
                device_ids=tuple(assignment.get("device_ids", ())),
            )
            for assignment in self._pending_assignments
        )

    async def _create_entry(
        self,
        path: str,
        title: str | None = None,
        source_path: str | None = None,
    ) -> config_entries.ConfigFlowResult:
        await self.async_set_unique_id(
            f"{source_path or path}:"
            f"{slugify(self._configuration_name, separator='_')}"
        )
        self._abort_if_unique_id_configured()
        svg_path = _source_path(self.hass, source_path or path)
        assignments = self._build_assignments()
        await self.hass.async_add_executor_job(
            save_configuration,
            svg_path,
            self._configuration_name,
            assignments,
        )
        data = {
            CONF_SVG_PATH: path,
            CONF_SOURCE_SVG_PATH: source_path or path,
            CONF_CONFIGURATION_NAME: self._configuration_name,
        }
        return self.async_create_entry(
            title=title or f"{Path(path).stem} {self._configuration_name}",
            data=data,
        )

    async def _update_reconfigured_entry(
        self, source_path: Path
    ) -> config_entries.ConfigFlowResult:
        """Save the JSON configuration and reload the existing entry."""
        entry = self._reconfigure_entry
        source_relative_path = source_path.relative_to(
            Path(self.hass.config.config_dir).resolve()
        ).as_posix()
        data = {
            **entry.data,
            CONF_SVG_PATH: source_relative_path,
            CONF_SOURCE_SVG_PATH: source_relative_path,
            CONF_CONFIGURATION_NAME: self._configuration_name,
        }
        return self.async_update_reload_and_abort(entry, data=data)
