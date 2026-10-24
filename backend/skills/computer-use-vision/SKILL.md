---
name: computer-use-vision
description: Use for desktop screenshots, visual inspection, or user-requested clicks and scrolling at visually identified points.
---

# Computer Use Vision

Physical input is permitted only for an interaction requested by the user.
For a screenshot-only or inspection-only request, return the requested
observation and stop; do not click or scroll. A new task means a new user goal,
not each tool turn or screenshot.

Use the user's requested target and action from context, and choose its point
from the returned images. Call the bare executable name `vision_desktop` through
`exec_command(command: str)`, without a file path, shell wrapper or pipeline.

## Normal sequence: observe → inspect → interact

For a user-requested interaction, use one desktop call per Agent turn and wait
for its returned image before the next step. The normal sequence has three steps:

```text
# 1. Capture the current desktop and visually locate the requested feature.
exec_command("vision_desktop observe")

# 2. From that overview, choose its rough point and a supported zoom.
exec_command("vision_desktop inspect_region --x <overview-x> --y <overview-y> --zoom <chosen-zoom>")

# 3. From the returned detail, identify the requested interaction point and act.
exec_command("vision_desktop interact_detail --x <detail-x> --y <detail-y> --action <requested-action>")
```

Angle-bracket fields are non-executable placeholders. Replace coordinates
only with values visually determined from the current image. Bare `observe`
needs no target text, action or reason; choose the action at `interact_detail`.
Choose zoom freely from `2,4,6,8`; omission defaults to `4`. Smaller zoom shows
more context, larger zoom narrows the crop. Choose enough context to identify
the requested interaction feature; the whole surrounding object need not fit.

**For a user-requested interaction, if its point or feature is identifiable in
the latest detail image, the next desktop call MUST be `interact_detail`.** Determine its
precise point from that image. Do not obtain another overview or crop merely
for perfect centering or a different zoom. A clipped
outer object does not prevent acting at its identifiable requested point.
For a click, use the requested clickable feature/point; for scrolling, use the
requested area under the pointer. Do not snap the point to a grid intersection （不要倾向于 将 交互点 吸附到网格交点上）.

## Exceptional recovery: requested point cannot be located

Only recover when the requested interaction point itself is absent, cut off
or unidentifiable in the detail. Then call bare `vision_desktop observe` for a
fresh overview BEFORE another `inspect_region`. Do not derive overview
coordinates from detail or reuse a remembered point from an older screenshot.
Treat the failed crop as negative evidence and visually reassess the feature
in the new overview. Do not consecutively repeat the same failed `(x,y)` or
command, invent random offsets, or guess a click. If the requested point still
cannot be located, stop without input. Once it is identifiable in the next
detail, proceed to `interact_detail` rather than continuing recovery.

## Coordinates and guides

Use only the latest returned image for its stage. Do not supply session,
frame or crop IDs; use the coordinates in the examples above.

Both stages have independent relative axes: `x=0..1000` runs left to right and
`y=0..1000` runs top to bottom within the current image. The complete source frame becomes
the Stage-1 square before eight-division guides and a border are drawn, with
no text labels. The exact clipped source crop becomes the Stage-2 square
before its sixteen-division guides; there is no padding. Estimate relative
positions from the displayed image without compensating for its stretch or
calculating global/physical coordinates.

Stage-2 even interior guides carry values `125,250,375,500,625,750,875`;
odd guides do not carry their own values. X text rotates 90° counterclockwise
beside its vertical guide; Y text is horizontal beside its horizontal guide.
A repeated X label still names that vertical guide's X value, regardless of
how far down the image its text appears; a repeated Y label likewise names
its horizontal guide's Y value. Labels at the edges have the same meaning.
Interpolate between guides; do not treat a label's text position as its value
on the other axis.

## Actions and actual errors

Actions are `left_click`, `right_click`, `left_double_click`, `right_double_click`,
`scroll_up` or `scroll_down`. Scrolling accepts optional `--steps` from 1 through
5 (default 1):

```text
vision_desktop interact_detail --x <detail-x> --y <detail-y> --action scroll_up --steps 5
vision_desktop interact_detail --x <detail-x> --y <detail-y> --action scroll_down --steps 5
```

A step is one native discrete wheel unit, not a promised pixel distance. All
steps belong to one interaction. Do not supply steps for clicks.

`interact_detail` requires a successfully returned `inspect_region` image.
Use actual returned errors to choose recovery; `OBSERVE_REQUIRED` requires a
new overview, and `DETAIL_REQUIRED` requires inspecting and viewing a detail
before interacting.

`ACTION_SENT` means input was dispatched, not that the application task
succeeded. Never replay a potentially delivered action or finish an uncertain
partial scroll by repeating input. A later intent starts with bare `observe`
and fresh images; a failed capture leaves the old detail ineligible. 
