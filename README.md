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
   metadata. For each element, choose one or more Home Assistant devices; their
   sensor and input number entities are then offered for assignment. Choose one
   or more entities to display on text, rectangles, circles, or ellipses;
   shape readings are added as text.
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

## Optional hand-authored SVG metadata

The setup flow does not require Home Assistant metadata: it creates the
per-element assignments described below. For advanced users, the integration
also continues to support hand-authored, room-group metadata. In this legacy
format, mark each room with a `<g>` element and set `data-ha-entity` to the
entity whose numeric state supplies its color and displayed value. Within the
group, use `data-ha-role="area"` for the colored shape, optional
`data-ha-role="glow"` for a fading glow, and `data-ha-role="value"` for text
that displays the value. Glow shapes fade from the entity color in the center
to transparent at the edges.

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

The room metadata attributes are optional except `data-ha-entity`; omitted
values use the defaults below. Colors must be six-digit hex colors. The
integration updates all marked area and value elements within a room group.

## Setup-generated SVG metadata

During setup, assignable elements need an SVG `id`; useful editor labels are
recommended so the flow can identify each element. Text elements, rectangles,
circles, and ellipses can display multiple entities. Shape readings are added
as text, one value and unit per line, without entity names. Automatic placement
uses the center for room-sized shapes and the top-left edge for floor-plan-sized
backdrops; you can choose a corner or center manually.

For each element, setup lets you select one or more devices and then choose
display and color entities from the selected devices. All eligible entities
are selected for display by default; a temperature entity is selected for color
when available. Numeric color-entity states use the legacy color scale above.
Leave the device selection empty, or skip an element during assignment, to use
neutral gray.
The flow uses labels from SVG editors such as Inkscape, falling back to the
parent layer label and then the SVG ID.

Assignments are embedded in the configured SVG, so the file can be reused
without repeating setup. The flow manages these attributes; they generally do
not need to be written by hand:

| Attribute | Default | Meaning |
| --- | --- | --- |
| `data-ha-display-entities` | none | Comma-separated entity IDs displayed on the element |
| `data-ha-display-position` | `auto` | Placement for readings: `auto`, `center`, `top-left`, `top-right`, `bottom-left`, or `bottom-right` |
| `data-ha-color-entity` | none | Entity whose numeric state controls this element's color |
| `data-ha-color-mode` | none | `fill` for solid color or `fade` for color fading to transparent |
| `data-ha-neutral` | absent | `true` sets skipped elements to neutral gray |
| `data-ha-device-ids` | none | Comma-separated Home Assistant device IDs selected for this element |
| `data-ha-min` | `16` | Temperature at the cold-color endpoint |
| `data-ha-comfort` | `22` | Temperature at the comfort-color midpoint |
| `data-ha-max` | `28` | Temperature at the hot-color endpoint |
| `data-ha-color-cold` | `#3B82F6` | Color at/below the minimum |
| `data-ha-color-comfort` | `#22C55E` | Color at the comfort point |
| `data-ha-color-hot` | `#EF4444` | Color at/above the maximum |
| `data-ha-unit` | `°C` | Suffix appended to the displayed sensor value |

Values between color stops are linearly interpolated. Unknown or non-numeric
sensor states are shown as `—` and use the neutral gray `#808080`.

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
