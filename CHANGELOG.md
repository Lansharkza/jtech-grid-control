# Changelog

## v53 — 2026-07-28

Solar Assistant integration plus a run of reliability fixes around scheduling,
reconnection, and session recording.

### Added
- **Solar Assistant integration.** Optional. Live inverter panel (battery,
  solar, load, grid, work mode) with fill-bar gauges, driven off Solar
  Assistant's local REST API. No cloud, no extra dependency.
- **Solar-aware charging.** While a car is charging, the inverter shares solar
  to the car when solar output and battery SOC are above thresholds
  (default 5 kW / 40%, adjustable on the panel), and protects the battery
  otherwise. Re-evaluated every 30 seconds so it follows the sun.
- Manual inverter work-mode buttons with the active mode highlighted (solid
  green), and an on/off toggle for the automation.
- **"On plug-in" as a schedule override.** Leave your schedule configured and
  press "On plug-in" to charge now at full power at any hour (e.g. grid-powered
  preconditioning); the schedule's out-of-window current cap is lifted while in
  this mode and restored when you switch back to "In window". The saved schedule
  is never deleted.
- Usage graphs label each bar with its exact value.

### Fixed
- **Schedules now use the configured timezone explicitly**, resolved from the
  timezone database rather than the container's ambient clock. A container
  running on UTC no longer evaluates the window hours off and stops a charge
  mid-window with "schedule window closed".
- **No session overwrite across restarts.** Transaction ids resume above the
  highest already recorded, and the in-place update matches on start time too,
  so a restart can't make a new session overwrite an older one.
- **Inverter no longer stranded on a disconnect.** If the charger drops
  mid-session, the inverter returns to its idle mode; a background safety net
  also corrects a stuck mode within 30 seconds.
- **Charger comes back online by itself.** On reconnect the server sets the
  charger operative and requests a status report, so the connector shows online
  after a container restart without clicking "Bring online". A deliberate "take
  offline" is remembered and respected.
- **Autostart recovers from transient charger states** ("Available",
  "Finishing") at the window boundary instead of silently missing the window.
- **Rejected autostarts back off** (30 min) instead of retrying every two
  minutes, so a car sitting full in SuspendedEV no longer floods the log; the
  back-off clears on unplug or a mode change.
- Corrected the inverter battery-power sign (this unit reports negative while
  discharging).

### Changed
- Inverter card polish: plain-language "what it's doing now" readout, work mode
  on the panel.
- Usage graphs: narrower plain-number y-axis, exact value above each bar, and no
  more session list under the graphs (redundant with the bars).
- Removed the coloured accent lines on the stat and summary cells for
  readability.

## Earlier

Initial public release: OCPP 1.6J central system and dashboard for the Teltonika
TeltoCharge EVC series — live control, scheduling, auto-start, charging modes,
permanent session history with usage graphs, HTTPS, sign-in, and a strict CSP.
