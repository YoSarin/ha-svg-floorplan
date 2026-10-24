"""Config flow for SVG Floorplan Image."""

from __future__ import annotations

import logging
from pathlib import Path

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.helpers import selector

from . import _source_path
from .const import CONF_ELEMENT_BINDINGS, CONF_SVG_PATH, DOMAIN
from .renderer import (
    AssignableElement,
    ElementAssignment,
    FloorplanError,
    FloorplanRenderer,
)

_LOGGER = logging.getLogger(__name__)
_ROLES = ["area", "value", "glow"]


def _error_key(error: FloorplanError) -> str:
    """Map validation failures to actionable config-flow messages."""
    message = str(error)
    if message.startswith("SVG path must"):
        return "invalid_path"
    if message.startswith("SVG file does not exist"):
        return "file_not_found"
    if message.startswith("SVG exceeds"):
        return "file_too_large"
    if message.startswith("SVG is not valid XML"):
        return "malformed_xml"
    if message.startswith("The root XML element"):
        return "not_svg"
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
    return "invalid_metadata"


def _default_role(element: AssignableElement) -> str:
    return "value" if element.tag == "text" else "area"


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
            path = user_input[CONF_SVG_PATH].strip()
            try:
                source_path = _source_path(self.hass, path)
                renderer = await self.hass.async_add_executor_job(
                    FloorplanRenderer, source_path
                )
                if renderer.entity_ids:
                    await self.hass.async_add_executor_job(renderer.render, {})
                    return await self._create_entry(path, ())
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
                    self._pending_assignments: list[ElementAssignment] = []
                    self._assignment_index = 0
                    return await self.async_step_assign()

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_SVG_PATH): str,
                }
            ),
            errors=errors,
            description_placeholders={"details": details},
        )

    async def async_step_assign(
        self, user_input: dict[str, str] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Assign an entity and rendering role to each SVG element."""
        if user_input is not None:
            entity_id = user_input.get("entity_id")
            element = self._pending_elements[self._assignment_index]
            if entity_id:
                self._pending_assignments.append(
                    ElementAssignment(
                        element_id=element.element_id,
                        entity_id=entity_id,
                        role=user_input["role"],
                    )
                )
            self._assignment_index += 1

            if self._assignment_index >= len(self._pending_elements):
                if not self._pending_assignments:
                    self._assignment_index = 0
                    return self._show_assignment_form("no_entity_assigned")

                try:
                    source_path = _source_path(self.hass, self._pending_path)
                    renderer = await self.hass.async_add_executor_job(
                        FloorplanRenderer,
                        source_path,
                        tuple(self._pending_assignments),
                    )
                    await self.hass.async_add_executor_job(renderer.render, {})
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
                        if assignment.element_id != last_element_id
                    ]
                    return self._show_assignment_form(
                        _error_key(err)
                        if isinstance(err, FloorplanError)
                        else "file_read_error",
                        str(err) if isinstance(err, FloorplanError) else "",
                    )
                return await self._create_entry(
                    self._pending_path, tuple(self._pending_assignments)
                )

        return self._show_assignment_form()

    def _show_assignment_form(
        self, error: str | None = None, details: str = ""
    ) -> config_entries.ConfigFlowResult:
        element = self._pending_elements[self._assignment_index]
        errors = {"base": error} if error else {}
        return self.async_show_form(
            step_id="assign",
            data_schema=vol.Schema(
                {
                    vol.Optional("entity_id"): selector.EntitySelector(
                        selector.EntitySelectorConfig(
                            domain=["sensor", "input_number"]
                        )
                    ),
                    vol.Optional("role", default=_default_role(element)): (
                        selector.SelectSelector(
                            selector.SelectSelectorConfig(
                                options=_ROLES,
                                mode=selector.SelectSelectorMode.DROPDOWN,
                            )
                        )
                    ),
                }
            ),
            errors=errors,
            description_placeholders={
                "element_id": element.element_id,
                "tag": element.tag,
                "details": details,
            },
        )

    async def _create_entry(
        self, path: str, assignments: tuple[ElementAssignment, ...]
    ) -> config_entries.ConfigFlowResult:
        await self.async_set_unique_id(path)
        self._abort_if_unique_id_configured()
        return self.async_create_entry(
            title=Path(path).stem,
            data={
                CONF_SVG_PATH: path,
                CONF_ELEMENT_BINDINGS: [
                    {
                        "element_id": assignment.element_id,
                        "entity_id": assignment.entity_id,
                        "role": assignment.role,
                    }
                    for assignment in assignments
                ],
            },
        )
