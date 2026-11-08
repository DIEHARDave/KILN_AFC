# Running AFC-Klipper-Add-On on Kiln OS: findings

What it took to run the full ArmoredTurtle AFC (pinned at
`AFC_COMMIT`) on Kiln OS with the Flashforge IFS as an AFC unit, and what
could move upstream so the Kiln-specific part shrinks to configuration.

Status: emulator-tested (`tests/test_kiln_afc.py`, 22 checks against
Kiln's real `ifs.py` and a simulated IFS); not yet run on a printer.

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

## Open questions (hardware)

- IFS runout during a print: AFC's runout (infinite spool / pause) and
  Kiln's `IFS_AUTO_REFILL` both react. Probably one of them should be off.
- AFC's PREP while the IFS still reports slots empty after boot
  (`stable_polls` covers the poll, PREP reads once).
- Swaps on a real print: AFC's saved position and z handling around
  Kiln's purge-box moves.
- HelixScreen, Mainsail and Fluidd AFC panels with the real AFC.
