# spoolman plugin: a Spoolman spool per IFS slot
#
# SPOOLMAN_SET_SLOT SLOT=1-4 SPOOL=<id> (SPOOL= empty clears) stores which
# spool is in each slot (save variable spoolman_slots). Each time the IFS
# loads a slot (Kiln OS event "ifs:loaded"), that slot's spool becomes
# Moonraker's active spool (remote method spoolman_set_active_spool, from
# Moonraker's [spoolman]); emptying the nozzle clears it. While no slot
# has a spool, the active spool is left alone. SPOOLMAN_SLOTS lists them;
# printer.spoolman_slots.slots holds them ("1"-"4": id or null).
#
# This file may be distributed under the terms of the GNU GPLv3 license.
import logging

SLOTS = (1, 2, 3, 4)
VARIABLE = "spoolman_slots"


class SpoolmanSlots:
    def __init__(self, config):
        self.printer = config.get_printer()
        self.gcode = self.printer.lookup_object('gcode')
        self.spools = [None] * len(SLOTS)
        self.printer.register_event_handler("klippy:connect", self._load)
        self.printer.register_event_handler("ifs:loaded", self._loaded)
        self.gcode.register_command(
            "SPOOLMAN_SET_SLOT", self.cmd_SPOOLMAN_SET_SLOT,
            desc="Spoolman spool in an IFS slot: SLOT= SPOOL= (empty clears)")
        self.gcode.register_command(
            "SPOOLMAN_SLOTS", self.cmd_SPOOLMAN_SLOTS,
            desc="List the Spoolman spool of each IFS slot")

    def _load(self):
        sv = self.printer.lookup_object('save_variables', None)
        v = sv.allVariables.get(VARIABLE) if sv is not None else None
        if isinstance(v, (list, tuple)) and len(v) == len(SLOTS):
            self.spools = [s if isinstance(s, int) and not isinstance(s, bool)
                           and s > 0 else None for s in v]

    def _activate(self, slot):
        if not any(self.spools):
            return
        sid = self.spools[slot - 1] if slot else None
        webhooks = self.printer.lookup_object('webhooks')
        try:
            webhooks.call_remote_method("spoolman_set_active_spool",
                                        spool_id=sid)
        except Exception as e:
            # Moonraker without [spoolman], or not connected
            logging.info("spoolman_slots: active spool not set: %s", e)

    def _loaded(self, slot):
        self._activate(slot)

    def cmd_SPOOLMAN_SET_SLOT(self, gcmd):
        slot = gcmd.get_int('SLOT', minval=SLOTS[0], maxval=SLOTS[-1])
        raw = gcmd.get('SPOOL', '').strip()
        try:
            sid = int(raw) if raw else None
        except ValueError:
            raise gcmd.error("SPOOLMAN_SET_SLOT: bad SPOOL '%s'" % (raw,))
        if sid is not None and sid <= 0:
            raise gcmd.error("SPOOLMAN_SET_SLOT: bad SPOOL '%s'" % (raw,))
        self.spools[slot - 1] = sid
        if self.printer.lookup_object('save_variables', None) is not None:
            self.gcode.run_script_from_command(
                "SAVE_VARIABLE VARIABLE=%s VALUE=\"%r\"" % (VARIABLE,
                                                            self.spools))
        ifs = self.printer.lookup_object('ifs', None)
        if ifs is not None and ifs.get_status(0.)['channel'] == slot:
            self._activate(slot)
        gcmd.respond_info("Spoolman: slot %d -> %s" % (
            slot, "spool %d" % sid if sid else "no spool"))

    def cmd_SPOOLMAN_SLOTS(self, gcmd):
        gcmd.respond_info("\n".join(
            "slot %d: %s" % (s, "spool %d" % self.spools[s - 1]
                             if self.spools[s - 1] else "-") for s in SLOTS))

    def get_status(self, eventtime):
        return {'slots': {str(s): self.spools[s - 1] for s in SLOTS}}


def load_config(config):
    return SpoolmanSlots(config)
