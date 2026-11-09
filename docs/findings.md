# Running AFC-Klipper-Add-On on Kiln OS: findings

What it took to run the full AFC-Klipper-Add-On (pinned at
`AFC_COMMIT`, AFC's DEV branch since 2026-11-08) on Kiln OS with the Flashforge IFS as an AFC unit, and what
could move upstream so the Kiln-specific part shrinks to configuration.

Status: emulator-tested (`tests/test_kiln_afc.py`, 22 checks against
Kiln's real `ifs.py` and a simulated IFS). On an AD5X (Kiln, AFC 5ea84ea)
since 2026-11-08: PREP, idle unload and load work; no print yet.

## How it works today

- `extras/kiln_afc.py` (bootstrap): adds `afc_ifs/` and AFC's `extras/`
  to `extras.__path__`, wraps `printer.load_object` so AFC's sections and
  helper objects load from there, and adds `IFS` to AFC's unit-type lists.
- `afc_ifs/AFC_IFS.py` (unit): the IFS has its own motors and firmware;
  Kiln's `ifs.py` drives it. The unit uses AFC's `unit_load_lane` /
  `unit_unload_lane` hooks (as OpenAMS does) to run `IFS_LOAD`,
  `IFS_UNLOAD`, `IFS_SWAP` and `IFS_EJECT`, feeds the lane sensors from
  `printer.ifs.loaded_channels`, mirrors Kiln's `ifs.channel` into AFC,
  and syncs lane material/color with Kiln's `ifs_materials` both ways.
- `plugin.cfg`: AFC's cut, park, poop, kick, wipe, tip forming and z_hop
  are off (Kiln's IFS commands do that work); `force_assign_map` hands
  T0-T3 to AFC (Kiln's macros become `_T0`-`_T3`).

## AFC DEV (3973c77) vs main

- Fixed on DEV: `send_lane_data` without a Moonraker client (before PREP);
  several T(n) per lane (`enable_multiple_mapping`, `lane.map` is a list).
  AFC_IFS now replays Kiln's tool map with `SET_MAP` instead of writing
  `afc.tool_cmds`.
- New bug on DEV: a lane's config `map:` stays the tuple from
  `config.getlist()` (`AFC_lane.__init__`), so the first multi-mapping
  `SET_MAP` onto it raises `tuple + list` and shuts Klipper down. Maps
  restored from AFC.var are lists. AFC_IFS converts its lanes at PREP.
- lane_data is keyed by T(n) on DEV (`T0`..), Kiln's kiln_lanes keeps
  writing `lane1`..`lane4`: Orca gets both sets, and they disagree while a
  print's tool map differs from the slots.

## Candidates for AFC upstream

1. **Unit traits instead of hardcoded type lists.** `AFC_lane.ONLY_LOAD_TYPES`,
   `AFC_lane.EXCLUDE_TYPES` and `AFC_unit.SENSORLESS_UNITS` name unit types
   ("HTLF", "OpenAMS", ...). A new unit has to patch these lists at import
   time. Proposal: class attributes on `afcUnit` (e.g. `only_load_sensor`,
   `sensorless`), with the lists kept as a fallback.
2. **Per-unit opt-out of AFC's pre-heat.** `AFC._check_extruder_temp`
   heats and waits before every load and unload. Units whose own load
   heats for the material (the IFS through Kiln, likely ACE and U1 too)
   only get an extra wait. Proposal: a unit attribute, e.g.
   `manages_temperature`, that `_check_extruder_temp` respects. AFC_IFS
   wraps the method for now.
3. **A tool-change hook.** `CHANGE_TOOL` is `TOOL_UNLOAD` + `TOOL_LOAD`.
   Units that do a change in one operation (Kiln's `IFS_SWAP`: cut,
   unload, load and flush with the slot's flush volume) must infer it in
   `unit_unload_lane` from `afc.next_lane_load` and `in_print()`, and defer
   to `unit_load_lane`. Proposal: an optional `unit_change_lane(old, new)`
   called by `CHANGE_TOOL` when both lanes are in the same unit.
4. **Loading from outside klippy/extras.** AFC assumes its modules are
   linked into `klippy/extras` (install-afc.sh) and uses
   `printer.load_object()` for its own helpers (`AFC_spool`, `AFC_error`,
   `AFC_functions`, ...). On a read-only Klipper (Kiln's squashfs) that
   needs the bootstrap above. Nothing to change in AFC necessarily, but
   worth a note in the docs for unit authors and for firmware projects.
5. **Docs for unit authors:** a hook-only unit must call
   `lane.send_lane_data()` in `system_Test` (AFC deletes `lane_data` at
   PREP), set `lane._load_state = False` for a virtual hub (AFC_lane
   defaults it to True without a load pin, which makes the hub "not
   clear"), and set `stepperless_drive`.
6. **The unit itself.** `AFC_IFS` depends only on an `ifs` object with
   Kiln's status fields and commands, and its commands are templates
   (`load_cmd`, `unload_cmd`, `swap_cmd`, `eject_cmd`). It could live in
   AFC as the IFS unit, with a test in AFC's suite; other IFS firmwares
   (Zmod's zmod_color) would need a status adapter.

## Candidates for Kiln OS

1. **Plugin loader:** let a plugin declare module folders that go on
   `extras.__path__` and that `load_object` searches (and accept
   AFC-style module names such as `AFC_lane`). Then `kiln_afc.py` is not
   needed.
2. **One lane_data writer:** `kiln_lanes` (Moonraker) and AFC both write
   `lane_data` `lane1`-`lane4`. With AFC_IFS syncing materials both write
   the same color and material, but `kiln_lanes` could stand down while
   Klipper has an `AFC` object (as HelixScreen switches backends).
3. **One owner of the slot's spool:** Kiln's optional spoolman plugin
   (`spoolman_slots`) keeps a spool per slot and sets Moonraker's active
   spool on every `ifs:loaded`; AFC keeps a spool per lane and sets it on
   its tool loads. Two writers of the active spool log usage against the
   wrong spool as soon as their lists differ (on AD5X-2 the plugin still
   had a stale spool for slot 4). AFC_IFS `sync_spools` now keeps both
   lists the same, but the plugin could stand down while Klipper has an
   `AFC` object, or read AFC's lane spools, and leave the active spool to
   AFC. If AFC became part of Kiln, the plugin would remain for printers
   without it.

## Hardware notes (AD5X-2)

- PREP matches the IFS: loaded slots, the slot in the toolhead, and Kiln's
  slot materials and colors in the lanes and in `lane_data`.
- `T0` with lane1 empty in the head: AFC -> `IFS_LOAD SLOT=1`, 130 s, both
  sides agree afterwards. An idle change lane1 -> lane4: the
  `IFS_UNLOAD SLOT=1` worked (118 s); the load of slot 4 failed in Kiln
  ("filament did not reach the toolhead", 3 retries), a filament-path
  problem on that slot, not AFC.
- AFC wants the printer homed before a tool load; Kiln's IFS commands do
  too, so nothing is lost.
- AFC renames and wraps Kiln's `PAUSE`, `RESUME`, `UNLOAD_FILAMENT`,
  `M104` and `M109`; to be checked in a print.
- AFC starts its Moonraker client only when PREP runs (the `afc_welcome`
  delayed gcode), so a unit's timers must not call `send_lane_data` before
  then (fixed in AFC_IFS: it waits for each lane's PREP).
- Outside a print a failed unit move doesn't pause, and then nothing takes
  AFC out of Loading; AFC_IFS sets AFC's error state itself (cleared by
  `RESET_FAILURE`). Worth handling in AFC for all hook units.
- Tool maps: Kiln maps a print's tools to slots by color
  (`IFS_MAP_MODE loaded`), AFC kept T1 -> lane2 and paused on an empty
  lane. AFC_IFS now writes Kiln's `ifs.tool_map` into `afc.tool_cmds`
  (`SET_MAP` can't express two tools on one slot). Upstream candidate: a
  unit hook to resolve T<n>, or tool_cmds allowing many tools per lane.
- Second cone print: T1 -> slot 4 by Kiln's color matching, one
  `IFS_SWAP CHANNEL=4` (136 s), no help needed. Assigning a Spoolman spool
  from the Fluidd AFC panel mid-print worked.
- HelixScreen caches the AFC lane names at its start: after switching from
  AFCLite (lanes E0-E3) its print-end `SET_WEIGHT LANE=E1 ...` failed until
  HelixScreen was restarted. It also looks for `AFC/AFC.cfg`,
  `AFC_Macro_Vars.cfg` and Moonraker's `afc-install` namespace (written by
  AFC's installer), which a plugin install doesn't have; only warnings.
- Mainsail's AFC panel: a lane card is as wide as its `T0 > LANE1` header
  chip (no-wrap), so four lanes wrap 3 + 1 in a normal column; with
  AFCLite's names (`T0 > E0`) they fit. Tested in the live page by
  shortening the chips. Candidate for Mainsail: let the chip shrink or
  ellipsize. Lane names stay `lane1`-`lane4` (lane_data keys, Kiln's
  kiln_lanes writes the same keys).
- Kiln: `FIRMWARE_RESTART`/`RESTART` keep the Python process, so an updated
  plugin module only loads after restarting the Klipper service
  (`/etc/init.d/S61klipper restart`). Plugin updates need a note on this.

## Open questions (hardware)

- IFS runout during a print: AFC's runout (infinite spool / pause) and
  Kiln's `IFS_AUTO_REFILL` both react. Probably one of them should be off.
- AFC's PREP while the IFS still reports slots empty after boot
  (`stable_polls` covers the poll, PREP reads once).
- Swaps on a real print: AFC's saved position and z handling around
  Kiln's purge-box moves.
- HelixScreen, Mainsail and Fluidd AFC panels with the real AFC.
