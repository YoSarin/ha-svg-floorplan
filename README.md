# SVG Floorplan Image

A Home Assistant custom integration that reads a user-provided SVG floor plan,
updates configured elements from Home Assistant entities, and exposes the result as
SVG and PNG image entities.

The SVG is configuration, not integration-owned content. The repository includes
only a small example.

## Preparing a floor plan

Keep the SVG simple. You do not need to add Home Assistant metadata, choose
sensors, or configure colors in an SVG editor; the integration setup flow lets
you assign entities and choose how each shape displays and colors them.

A practical layout is a transparent bitmap floor-plan image as the bottom
layer, with individual rooms or regions drawn above it as simple SVG shapes
(for example, rectangles or polygons). Give each shape a meaningful name or
label so it is easy to identify during setup. Keep these areas as separate
shapes rather than baking their fills and labels into one complex SVG drawing.
The transparent parts of the bitmap allow the independently colored shapes to
show through. The bitmap can be exported from a drawing or floor-plan tool as a
transparent PNG and placed in the SVG as its background.

During setup, name an output configuration, choose the device that provides
the entities, then configure each named shape: select values to display, choose
an entity for its color, or skip it to use neutral gray. Settings are stored in
a JSON sidecar; the source SVG remains unchanged.

## Install

Add this GitHub repository to HACS as a custom repository with the
**Integration** category, install **SVG Floorplan Image**, and restart Home
Assistant.

For local testing, copy `custom_components/svg_floorplan` into

`<HA config>/custom_components/svg_floorplan` and restart Home Assistant.

The integration uses `resvg-py` to rasterize PNG images. Home Assistant installs
the pinned Python dependency from the integration manifest. DejaVu Sans fonts
are bundled so SVG text also renders in minimal Home Assistant containers with
no system fonts installed.

## Configure

1. Put your SVG under the Home Assistant configuration directory, for example
   `<HA config>/floorplans/house.svg`.
2. In **Settings → Devices & services**, add **SVG Floorplan Image**.
3. Enter the SVG path relative to the configuration directory:
   `floorplans/house.svg`.
4. Enter a name for this output configuration (for example, `temperature`).
   Repeat the add-integration flow with the same SVG and a different name to
   create another independent image pair, such as a `humidity` view.
5. The setup flow walks through identified shapes and text elements by their
   SVG editor labels or IDs. It does not require pre-authored Home Assistant
   metadata. For each element, choose one or more Home Assistant devices; their
   sensor and input number entities are then offered for assignment. Choose one
   or more entities to display on text, rectangles, circles, ellipses,
   polygons, or straight-edged paths; shape readings are added as text in
   selection order.
   Automatic placement centers readings in room shapes and places readings
   for floor-plan-sized backdrops near their top-left edge; you can override
   the position. Enable **Improve reading contrast** to add a strong, soft
   dark halo on detailed backgrounds; it replaces the hard text outline.
   Independently, choose an entity to control the element's
   color, which defaults to solid color. A numeric entity state is mapped
   through the color scale; fading to neutral gray along the shape edges or
   leaving the element uncolored are also available. Skipping an element gives
   its shape a neutral gray fill or its text a neutral gray foreground.
   Setup saves the assignments in a JSON sidecar beside the SVG (for example,
   `floorplans/house.json`) without writing Home Assistant configuration into
   the SVG.
6. Restart or reload the integration after changing the source SVG.

To change assignments later, open the integration entry in **Settings →
Devices & services**, use its menu, and select **Reconfigure**. The current
assignments are preselected where possible; reconfiguration updates that
named configuration in the JSON sidecar and reloads the entry.

Each named configuration creates SVG and PNG entities using the pattern
`image.floorplan_{svgName}_{configurationName}_{fileExtension}` (for example,
`image.floorplan_house_temperature_png` and
`image.floorplan_house_humidity_png`). Use the PNG image entity with the Home
Assistant Companion app's Android Picture widget.

## JSON configuration

During setup, assignable elements need an SVG `id`; useful editor labels are
recommended so the flow can identify each element. Text elements, rectangles,
circles, ellipses, polygons, and straight-edged paths can display multiple
entities. Shape readings are added as text, one value and unit per line,
without entity names. All available entities are selected by default; reorder
the selected entities in the selector to choose their reading order.
Automatic
placement uses the shape's center for room-sized shapes and the top-left edge
for floor-plan-sized backdrops; you can choose a corner or center manually.
**Improve reading contrast** adds a strong, soft dark halo around readings on
complex backgrounds. When enabled, it removes the hard text outline.

For each element, setup lets you select one or more devices and then choose
display and color entities from the selected devices. All eligible entities
are selected for display by default; a temperature entity is selected for color
when available. Numeric color-entity states use the scale described below.
Leave the device selection empty, or skip an element during assignment, to use
neutral gray.
The flow uses labels from SVG editors such as Inkscape, falling back to the
parent layer label and then the SVG ID.

The sidecar JSON stores configurations by name, with an assignment list for
each SVG element. Adding another configuration to the same SVG preserves
existing configurations. For example, `floorplans/house.json` has this shape:

```json
{
  "version": 1,
  "configurations": {
    "temperature": {
      "assignments": [
        {
          "element_id": "living-room",
          "display_entity_ids": ["sensor.living_room_temperature"],
          "display_position": "auto",
          "improve_readability": false,
          "color_entity_id": "sensor.living_room_temperature",
          "color_mode": "fill",
          "neutralize": false,
          "device_ids": []
        }
      ]
    },
    "humidity": {
      "assignments": [
        {
          "element_id": "living-room",
          "display_entity_ids": ["sensor.living_room_humidity"],
          "display_position": "auto",
          "color_entity_id": "sensor.living_room_humidity",
          "color_mode": "fill",
          "neutralize": false,
          "device_ids": []
        }
      ]
    }
  }
}
```

The flow writes and validates this file. Do not add Home Assistant
configuration metadata to the SVG.

Numeric color-entity states use a cool-to-warm weather palette: blue at -20,
green at 10, yellow at 25, and red at 40 and above. Values between these stops
are linearly interpolated, so mild temperatures such as 12 remain in the
blue/green range rather than becoming orange. Unknown or non-numeric sensor
states use the neutral gray `#808080`.

The [`examples/example_house.svg`](examples/example_house.svg) file shows a
simple house layout with separately labeled areas and a bitmap outline. It has
no Home Assistant metadata: add it through the integration and assign entities
to areas during setup. Copy it into your HA config directory to try the flow.

## Development

Run the renderer tests with:

```sh
python -m pip install -r requirements_test.txt
python -m pytest
```

Every push to `main` runs the tests and Python compilation. If they pass, the
release workflow increments the integration's patch version, pushes a matching
`v*` tag, and publishes a GitHub release.
