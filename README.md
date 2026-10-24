# SVG Floorplan Image

A Home Assistant custom integration that reads a user-provided SVG floor plan,
updates marked elements from Home Assistant entities, and exposes the result as
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

During setup, choose the device that provides the entities, then configure
each named shape: select values to display, choose an entity for its color, or
skip it to use neutral gray. The integration creates a configured SVG copy;
the source SVG remains unchanged.

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
4. The setup flow walks through identified shapes and text elements by their
   SVG editor labels or IDs. It does not require pre-authored Home Assistant
   metadata. First choose a Home Assistant device; its sensor and input number
   entities are then offered for assignment. Choose one or more entities to display on
   text, rectangles, circles, or ellipses; shape readings are added as text.
   Automatic placement centers readings in room shapes and places readings
   for floor-plan-sized backdrops near their top-left edge; you can override
   the position. Independently, choose an entity to control the element's
   color, which defaults to solid color. A numeric entity state is mapped
   through the color scale; fading to transparent and leaving the element
   uncolored are also available. Skipping an element gives its shape a neutral
   gray fill or its text a neutral gray foreground.
   Setup saves a configured copy beside the original (for example
   `house.configured.svg`) with reusable sensor metadata embedded in the SVG.
5. Restart or reload the integration after changing the source SVG.

To change assignments later, open the integration entry in **Settings →
Devices & services**, use its menu, and select **Reconfigure**. The current
assignments are preselected where possible; reconfiguration writes an updated
configured SVG and reloads the entry.

The integration creates two image entities: one SVG and one PNG. Use the PNG
image entity with the Home Assistant Companion app's Android Picture widget.

## SVG metadata

Mark each room with a `<g>` element. Set `data-ha-entity` to the entity whose
state supplies the displayed value. Inside that group, mark the shape to color
with `data-ha-role="area"`, an optional fading temperature glow with
`data-ha-role="glow"`, and the text element to update with
`data-ha-role="value"`. Glow shapes use a radial gradient that fades from the
temperature color in the center to transparent at the edges.

```xml
<g
  id="living-room"
  data-ha-entity="sensor.teplomer_obyvak_temperature"
  data-ha-min="16"
  data-ha-comfort="22"
  data-ha-max="28"
  data-ha-color-cold="#3B82F6"
  data-ha-color-comfort="#22C55E"
  data-ha-color-hot="#EF4444"
  data-ha-unit="°C"
>
  <rect id="living-room-area" data-ha-role="area"
        x="50" y="50" width="300" height="300"/>
  <rect id="living-room-glow" data-ha-role="glow"
        x="25" y="25" width="350" height="350"/>
  <text id="living-room-value" data-ha-role="value"
        x="200" y="200" text-anchor="middle">—</text>
</g>
```

The metadata attributes are optional except `data-ha-entity`; omitted values
use the defaults below. Colors must be six-digit hex colors. The integration
updates all marked area and value elements within a room group.
Elements without metadata can instead be assigned during setup. Supported
elements need an SVG `id`. Text elements, rectangles, circles, and ellipses can
display multiple entities; shape readings are added as text, one value and unit
per line, without the entity name. Automatic placement uses the center for
room-sized shapes and the top-left edge for floor-plan-sized backdrops. You can
choose center or a corner manually. The color entity is selected independently.
During setup, a device is chosen first, and the display and color entity
selectors are limited to sensor and input number entities registered to it.
The setup flow uses labels from
SVG editors such as Inkscape to identify each element, falling back to its
parent layer label and then its SVG ID.

Assignments are saved on each configured element as SVG attributes:
`data-ha-display-entities` contains comma-separated entity IDs, while
`data-ha-color-entity` and `data-ha-color-mode` select the entity and either
`fill` or `fade`. Skipped elements are marked with `data-ha-neutral="true"`.
This means the generated configured SVG retains its behavior when reused or
added to another Home Assistant installation.

| Attribute | Default | Meaning |
| --- | --- | --- |
| `data-ha-min` | `16` | Temperature at the cold-color endpoint |
| `data-ha-comfort` | `22` | Temperature at the comfort-color midpoint |
| `data-ha-max` | `28` | Temperature at the hot-color endpoint |
| `data-ha-color-cold` | `#3B82F6` | Color at/below the minimum |
| `data-ha-color-comfort` | `#22C55E` | Color at the comfort point |
| `data-ha-color-hot` | `#EF4444` | Color at/above the maximum |
| `data-ha-unit` | `°C` | Suffix appended to the displayed sensor value |

Values between color stops are linearly interpolated. Unknown or non-numeric
sensor states are shown as `—` and use the neutral gray `#808080`.

An example matching the sensors discussed is in
[`examples/two_rooms.svg`](examples/two_rooms.svg). You can copy it into your
HA config directory and update the entities and layout.

## Development

Run the renderer tests with:

```sh
python -m pip install -r requirements_test.txt
python -m pytest
```
