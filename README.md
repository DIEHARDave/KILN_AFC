# KILN_AFC

ArmoredTurtle's [AFC-Klipper-Add-On](https://github.com/ArmoredTurtle/AFC-Klipper-Add-On)
on Kiln OS, with the AD5X IFS as an AFC unit. **Prototype: emulator-tested,
not yet run on a printer.** Replaces [KILN_AFCLite](https://github.com/DIEHARDave/KILN_AFCLite)
(the two conflict).

AFC owns the tool changes (T0-T3, `CHANGE_TOOL`, `TOOL_UNLOAD`,
`LANE_UNLOAD`, runout lanes, stats, the AFC panels). Kiln's IFS commands do
the filament work: `IFS_LOAD`, `IFS_UNLOAD`, `IFS_SWAP` (mid-print changes),
`IFS_EJECT`. Lane material and color stay in step with Kiln's
`ifs_materials` both ways.

How it fits together and what could move into AFC or Kiln:
[docs/findings.md](docs/findings.md).

## Install (manual for now)

```sh
cd /data/ad5x/plugins
su -s /bin/sh klipper -c "git clone https://github.com/DIEHARDave/KILN_AFC afc"
su -s /bin/sh klipper -c "sh /data/ad5x/plugins/afc/install.sh"   # clones AFC at AFC_COMMIT
```

Then `DISABLE_PLUGIN NAME=afclite` (if installed), `ENABLE_PLUGIN NAME=afc`,
and restart Klipper.

## Options

`[AFC_IFS IFS]` in local.cfg:

| Option | Default | |
|---|---|---|
| `use_swap` | True | Mid-print changes as one `IFS_SWAP` |
| `afc_heats` | False | Let AFC pre-heat before loads (Kiln heats anyway) |
| `sync_materials` | True | Sync lane material/color with `ifs_materials` |
| `poll_interval` | 1.0 | Seconds between IFS reads |
| `stable_polls` | 3 | Reads a slot change must hold |
| `head_sensor` | `adc_filament_sensor toolhead` | Toolhead sensor object |
| `load_cmd`, `unload_cmd`, `swap_cmd`, `eject_cmd` | `IFS_LOAD SLOT={slot}`, ... | Commands per move |

## Tests

```sh
python3 tests/test_kiln_afc.py --klipper ~/kiln-klipper \
    --afc ~/AFC-Klipper-Add-On --emu ~/KILN_IFSJacker/emulator \
    --python ~/klippy-test-env/bin/python
```

Linux (WSL works). Uses Kiln's `ifs.py` from `tests/kiln_files` and
KILN_IFSJacker's `fake_ifs.py` and `kiln_test_probe.py`.

GPLv3, like Klipper and AFC.
