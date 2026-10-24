"""Config flow for SVG Floorplan Image."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.helpers import selector

from . import _source_path
from .const import CONF_ELEMENT_BINDINGS, CONF_SVG_PATH, DOMAIN, MAX_SVG_BYTES
from .renderer import (
    ElementAssignment,
    FloorplanError,
    FloorplanRenderer,
)

_LOGGER = logging.getLogger(__name__)
_COLOR_MODES = [
    {"label": "Do not color this element", "value": "none"},
    {"label": "Solid temperature color", "value": "fill"},
    {"label": "Fade to transparent", "value": "fade"},
]


def _create_configured_copy(
    source_path: Path, assignments: tuple[ElementAssignment, ...]
) -> Path:
    """Write and validate a configured SVG beside its original."""
    source_renderer = FloorplanRenderer(source_path, assignments)
    source_renderer.render({})
    configured_svg = source_renderer.configured_svg(assignments)
    if len(configured_svg) > MAX_SVG_BYTES:
        raise FloorplanError(
            f"Configured SVG exceeds the {MAX_SVG_BYTES} byte size limit"
        )

    output_path = source_path.with_name(f"{source_path.stem}.configured.svg")
    suffix = 2
    while True:
        try:
            with output_path.open("xb") as configured_file:
                configured_file.write(configured_svg)
            break
        except FileExistsError:
            output_path = source_path.with_name(
                f"{source_path.stem}.configured-{suffix}.svg"
            )
            suffix += 1
        except OSError:
            output_path.unlink(missing_ok=True)
            raise

    try:
        configured_renderer = FloorplanRenderer(output_path)
        configured_renderer.render({})
    except (FloorplanError, OSError):
        output_path.unlink(missing_ok=True)
        raise
    return output_path


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
                    self._pending_assignments: list[dict[str, Any]] = []
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
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Choose displayed sensors and color behavior for one SVG element."""
        if user_input is not None:
            element = self._pending_elements[self._assignment_index]
            display_entity_ids = tuple(user_input.get("display_entities", ()))
            color_entity_id = user_input.get("color_entity")
            color_mode = user_input.get("color_mode", "none")
            if not user_input.get("skip"):
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
                        "color_entity_id": color_entity_id,
                        "color_mode": color_mode,
                    }
                )
            self._assignment_index += 1

            if self._assignment_index >= len(self._pending_elements):
                if not self._pending_assignments:
                    self._assignment_index = 0
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
                    configured_path = await self.hass.async_add_executor_job(
                        _create_configured_copy, source_path, assignments
                    )
                    _LOGGER.info("Saved configured SVG floor plan to %s", configured_path)
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
                return await self._create_entry(
                    configured_path.relative_to(
                        Path(self.hass.config.config_dir).resolve()
                    ).as_posix(),
                    (),
                    title=Path(self._pending_path).stem,
                )

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
        if element.tag in {"text", "rect", "circle", "ellipse"}:
            fields[vol.Optional("display_entities")] = selector.EntitySelector(
                selector.EntitySelectorConfig(
                    domain=["sensor", "input_number"], multiple=True
                )
            )
        fields.update({
            vol.Optional("color_entity"): selector.EntitySelector(
                selector.EntitySelectorConfig(domain=["sensor", "input_number"])
            ),
            vol.Optional("color_mode", default="none"): selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=_COLOR_MODES,
                    mode=selector.SelectSelectorMode.DROPDOWN,
                )
            ),
            vol.Optional("skip", default=False): bool,
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
        )

    def _build_assignments(self) -> tuple[ElementAssignment, ...]:
        return tuple(
            ElementAssignment(
                element_id=assignment["element_id"],
                display_entity_ids=tuple(assignment["display_entity_ids"]),
                color_entity_id=assignment["color_entity_id"],
                color_mode=assignment["color_mode"],
            )
            for assignment in self._pending_assignments
        )

    async def _create_entry(
        self,
        path: str,
        assignments: tuple[dict[str, Any], ...],
        title: str | None = None,
    ) -> config_entries.ConfigFlowResult:
        await self.async_set_unique_id(path)
        self._abort_if_unique_id_configured()
        return self.async_create_entry(
            title=title or Path(path).stem,
            data={
                CONF_SVG_PATH: path,
                CONF_ELEMENT_BINDINGS: list(assignments),
            },
        )
