# KILN_AFC

[AFC-Klipper-Add-On](https://github.com/AFCProject/AFC-Klipper-Add-On) (the AFC Project)
on Kiln OS, with the AD5X IFS as an AFC unit. **Testing release: running
on one AD5X since 2026-11-08 (idle changes, two-color prints, Spoolman).
Reports welcome in [Issues](https://github.com/DIEHARDave/KILN_AFC/issues).**
Replaces [KILN_AFCLite](https://github.com/DIEHARDave/KILN_AFCLite):
enabling this plugin disables and deletes afclite ([below](#coming-from-afclite)).

AFC owns the tool changes (T0-T3, `CHANGE_TOOL`, `TOOL_UNLOAD`,
`LANE_UNLOAD`, runout lanes, stats, the AFC panels). Kiln's IFS commands do
the filament work: `IFS_LOAD`, `IFS_UNLOAD`, `IFS_SWAP` (mid-print changes),
`IFS_EJECT`. Lane material and color stay in step with Kiln's
`ifs_materials` both ways, lane spools with Kiln's spoolman plugin
([Spoolman](#spoolman)).

How it fits together and what could move into AFC or Kiln:
[docs/findings.md](docs/findings.md).

## Install

Over SSH as root, with the printer idle:

```sh
cd /data/ad5x/plugins
su -s /bin/sh klipper -c "git clone https://github.com/DIEHARDave/KILN_AFC afc"
```

Then `ENABLE_PLUGIN NAME=afc` in the console. Klipper restarts, and on that
first start the plugin clones AFC (the AFC Project's repository, about 16 MB)
at the commit in `AFC_COMMIT`, so it takes a little longer. A tool load
or unload homes the printer first when it isn't homed (AFC's `auto_home`;
set `auto_home: False` under `[AFC]` in local.cfg to get AFC's "Please
home printer" error instead).

## Updates

Mainsail or Fluidd, Software Updates, entry **kiln-afc** (release tags
only), then `RESTART` with the printer idle. At that start the plugin
loads its new code, moves its AFC checkout to the release's `AFC_COMMIT`
(fetching it if needed) and reloads AFC. Kiln's Moonraker can't restart
Klipper itself, so the update doesn't.

By hand: `git pull` in `/data/ad5x/plugins/afc` as the klipper user, then
`RESTART`.

An `afc_path:` in `[kiln_afc]` points the plugin at an AFC checkout of your
own; that one is left as it is.

Don't run AFC's `install-afc.sh` on Kiln: Klipper is read-only there, and
this plugin does its job (module paths, config).

### Coming from AFCLite

Both plugins make the AFC object and `CHANGE_TOOL`, so they can't run
together. When KILN_AFC starts and finds afclite, it removes it:

- drops afclite from `plugins.cfg` and its update entry from
  `moonraker-plugins.conf` (Moonraker restarts),
- deletes `/data/ad5x/plugins/afclite`,
- if afclite was still enabled, stops this start with a message and
  restarts Klipper by itself 2 s later, without afclite.

Lane spools carry over: a lane AFC has no spool for takes the spool of
Kiln's spoolman slot, else the one afclite kept (`kiln_afclite_spool_*` in
save_variables; those variables stay). Lane names change from E0-E3 to
lane1-lane4: restart HelixScreen once (`/etc/init.d/S70helixscreen restart`)
so it drops the old names.

## Options

`[AFC_IFS IFS]` in local.cfg. `afc_ifs/AFC_IFS.py` is the shared IFS unit for Kiln OS and Zmod (Kiln and Zmod backends), proposed for AFC itself; until AFC ships it, this plugin carries a copy.

| Option | Default | |
|---|---|---|
| `use_swap` | True | Mid-print changes as one `IFS_SWAP` |
| `afc_heats` | False | Let AFC pre-heat before loads (Kiln heats anyway) |
| `sync_materials` | True | Sync lane material/color with `ifs_materials` |
| `sync_spools` | True | Sync lane Spoolman spools with Kiln's spoolman plugin (below) |
| `sync_map` | True | Route `T<n>` to the lane of the slot Kiln maps tool n to (`ifs.tool_map`); the old name `follow_tool_map` still works |
| `poll_interval` | 1.0 | Seconds between IFS reads |
| `stable_polls` | 3 | Reads a slot change must hold |
| `empty_polls` | 3 | Reads before an emptied slot counts |
| `firmware` | auto | `auto`, `kiln` or `zmod` (the unit also runs on Zmod) |
| `head_sensor` | `adc_filament_sensor toolhead` | Toolhead sensor object |
| `load_cmd`, `unload_cmd`, `swap_cmd`, `eject_cmd` | `IFS_LOAD SLOT={slot}`, ... | Commands per move |

## Spoolman

AFC keeps a Spoolman spool per lane (`SET_SPOOL_ID`, the AFC panels,
[FF_NFC](https://github.com/DIEHARDave/FF_NFC) tag scans) and makes it
Moonraker's active spool on each tool load, so usage is logged against
it. Kiln's optional spoolman plugin (`[spoolman_slots]`) does the same per
IFS slot on every IFS load. With `sync_spools`, the two stay in step: AFC's
spools win when Klipper starts, and later a spool set on either side
(`SET_SPOOL_ID` or `SPOOLMAN_SET_SLOT`) is copied to the other, so both
always pick the same active spool. A spool set on a slot while another
lane has it moves off that lane. If AFC refuses a spool (one Spoolman has
as empty, unless `[AFC] disable_weight_check`), the plugin's slot follows
AFC again.

Neither is needed for tracking itself: that is Moonraker's `[spoolman]`
in moonraker.conf.

## Tests

```sh
python3 tests/test_kiln_afc.py --klipper ~/kiln-klipper \
    --afc ~/AFC-Klipper-Add-On --emu ~/KILN_IFSJacker/emulator \
    --python ~/klippy-test-env/bin/python
```

`python3 tests/test_bootstrap.py` checks the afclite removal and the AFC
checkout following `AFC_COMMIT` on their own (no Klipper).

Linux (WSL works). Uses Kiln's `ifs.py` from `tests/kiln_files` and
KILN_IFSJacker's `fake_ifs.py` and `kiln_test_probe.py`.

GPLv3, like Klipper and AFC.
