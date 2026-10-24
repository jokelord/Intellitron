# Native X11 computer use

Implement X11 as a separate native transport under the existing capability,
authenticated sealed-frame IPC, geometry, calibration and input transaction
contracts. Preserve Wayland and all canonical Agent history/acceptance boundaries.

- Select explicit XDG_SESSION_TYPE=x11 or wayland first; without session type,
  prefer WAYLAND_DISPLAY, then DISPLAY. Never fall back from Wayland to XWayland.
- Open the authenticated X display, require XRandR/XTest and select the primary
  or sole monitor. Reject ambiguous multi-monitor selection.
- Capture the selected root region with XGetImage and convert to canonical RGB.
  This implementation does not use the historical v1.2 XShm proposal.
- Bind source=xrandr with root ID, monitor atom/name, output IDs and matching
  mapping_id/region_id xrandr:<root-id>:<monitor-atom>. Preserve Wayland evidence.
- Send XTest integer root-coordinate motion/button/wheel events. Revalidate
  root/monitor topology and geometry; invalidate stale bindings and crops. Keep
  bounded deadlines, cancellation, cleanup and shared seat serialization.
- Report X11 connection/mapping/input separately; Portal/EIS are not_applicable.
  DISPLAY and package discovery never imply readiness.

Verify native Xvfb capture/application button/scroll outcomes, geometry changes
and transport failure; collect a no-input real-host capture separately. Run
Python binding/calibration/dispatch/probe regressions, existing Wayland native
tests and unchanged Agent history/acceptance tests. A controlled target outcome
is separate from production calibration and release readiness.

Verified 2026-10-09: production no-input and input NativeClient probes on a private
640×480 Xvfb desktop exited 0. The input probe recorded one Tk down, up and wheel
callback; Portal/EIS were not_applicable, calibration not_run and ready false.
This establishes isolated transport/application evidence only.
