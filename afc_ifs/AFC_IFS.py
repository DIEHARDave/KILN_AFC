# AFC unit driver for the Flashforge IFS on Kiln OS.
#
# The IFS moves filament with its own firmware; Kiln's klipper/extras/ifs.py
# drives it and exposes printer.ifs plus the IFS_* commands. This unit hands
# AFC's load, unload and eject to those commands (the unit_load_lane /
# unit_unload_lane hooks AFC's OpenAMS unit uses), and feeds AFC's lane
# sensors from printer.ifs.loaded_channels and the toolhead sensor.
#
# Kiln's ifs.channel (the slot in the toolhead) is the truth for which lane
# is loaded: a load, unload or refill that did not go through AFC (screen,
# IFS_* by hand, IFS auto-refill) is mirrored into AFC's state.
#
# Copyright (C) 2026 DIEHARDave. GPLv3, like AFC.

from __future__ import annotations

import traceback
from configparser import Error as error

try: from extras.AFC_utils import ERROR_STR
except: raise error("Error when trying to import AFC_utils.ERROR_STR\n{trace}".format(trace=traceback.format_exc()))

try: from extras.AFC_lane import AFCLaneState
except: raise error(ERROR_STR.format(import_lib="AFC_lane", trace=traceback.format_exc()))

try: from extras.AFC_unit import afcUnit
except: raise error(ERROR_STR.format(import_lib="AFC_unit", trace=traceback.format_exc()))


class afcIFS(afcUnit):
    def __init__(self, config):
        super().__init__(config)
        self.type = config.get('type', 'IFS')
        self.stepperless_drive = True
        self.ifs_name = config.get('ifs', 'ifs')
        self.head_sensor_name = config.get('head_sensor', 'adc_filament_sensor toolhead')
        self.poll_interval = config.getfloat('poll_interval', 1.0, minval=0.2)
        # Readings a lane's slot must hold before AFC hears of a change (the
        # IFS reports slots empty and flapping for several seconds after boot)
        self.stable_polls = config.getint('stable_polls', 3, minval=1)
        # Mid-print tool changes as one IFS_SWAP (Kiln's cut, unload, load and
        # flush) instead of IFS_UNLOAD then IFS_LOAD
        self.use_swap = config.getboolean('use_swap', True)
        # Kiln's IFS commands heat the nozzle for the slot's material and put
        # the old target back; AFC's own pre-heat would only add a wait
        self.afc_heats = config.getboolean('afc_heats', False)
        self.load_cmd = config.get('load_cmd', 'IFS_LOAD SLOT={slot}')
        self.unload_cmd = config.get('unload_cmd', 'IFS_UNLOAD SLOT={slot}')
        self.swap_cmd = config.get('swap_cmd', 'IFS_SWAP CHANNEL={slot}')
        self.eject_cmd = config.get('eject_cmd', 'IFS_EJECT SLOT={slot}')

        self.ifs = None
        self.head_sensor = None
        self._slot = {}           # lane name -> IFS slot 1..4
        self._seen = {}           # lane name -> last reported presence
        self._pending = {}        # lane name -> (value, count) while settling
        self._busy = False        # our own IFS command is running
        self._swap_from = None    # lane whose unload was folded into a swap
        self._poll_timer = None
        self.logo = '<span class=success--text>IFS ready</span>\n'
        self.logo_error = '<span class=error--text>IFS not ready</span>\n'
        self.printer.register_event_handler('ifs:loaded', self._on_ifs_loaded)

    # -- setup ---------------------------------------------------------------

    def handle_connect(self):
        super().handle_connect()
        self.ifs = self.printer.lookup_object(self.ifs_name, None)
        if self.ifs is None:
            raise error("[AFC_IFS %s]: Kiln's [%s] object not found"
                        % (self.name, self.ifs_name))
        self.head_sensor = self.printer.lookup_object(self.head_sensor_name, None)
        if not self.afc_heats:
            self._skip_afc_preheat()
        for lane in self.lanes.values():
            slot = getattr(lane, 'index', 0)
            if not 1 <= slot <= 16:
                raise error("[%s]: unit must be %s:<slot 1-4>, e.g. unit: %s:1"
                            % (lane.fullname, self.name, self.name))
            self._slot[lane.name] = slot
            # One IFS sensor per lane, driven by the poll below. The virtual
            # hub reads raw_load_state; the IFS has no hub sensor, so it stays
            # clear (AFC_lane defaults it to True when no load pin is given).
            lane.prep_state = False
            lane._load_state = False
            lane.loaded_to_hub = False

    def _skip_afc_preheat(self):
        # AFC heats and waits before every load and unload; there is no
        # per-unit switch for it (upstream candidate), so wrap it per lane.
        afc = self.afc
        if getattr(afc, '_ifs_preheat_wrapped', False):
            return
        orig = afc._check_extruder_temp
        def check(cur_lane, *args, **kwargs):
            unit = getattr(cur_lane, 'unit_obj', None)
            if isinstance(unit, afcIFS) and not unit.afc_heats:
                return False
            return orig(cur_lane, *args, **kwargs)
        afc._check_extruder_temp = check
        afc._ifs_preheat_wrapped = True

    def handle_ready(self):
        super().handle_ready()
        self._poll_timer = self.reactor.register_timer(
            self._poll, self.reactor.monotonic() + self.poll_interval)

    # -- Kiln state ----------------------------------------------------------

    def _ifs_status(self, eventtime=None):
        if eventtime is None:
            eventtime = self.reactor.monotonic()
        try:
            return self.ifs.get_status(eventtime)
        except Exception:
            self.logger.debug("AFC_IFS: ifs.get_status failed\n" + traceback.format_exc())
            return {}

    def _present(self, st, lane):
        return self._slot[lane.name] in (st.get('loaded_channels') or [])

    def _head_detected(self, eventtime):
        if self.head_sensor is None:
            return False
        try:
            return bool(self.head_sensor.get_status(eventtime).get('filament_detected'))
        except Exception:
            return False

    def _lane_for_slot(self, slot):
        for name, s in self._slot.items():
            if s == slot:
                return self.lanes.get(name)
        return None

    def _set_presence(self, lane, present):
        lane.prep_state = present
        lane.loaded_to_hub = present
        lane._load_state = False

    def _poll(self, eventtime):
        if self._busy or self.ifs is None:
            return eventtime + self.poll_interval
        st = self._ifs_status(eventtime)
        if not st or st.get('busy'):
            return eventtime + self.poll_interval

        head = self._head_detected(eventtime)
        for lane in self.lanes.values():
            if lane.extruder_obj is not None and lane.extruder_obj.tool_start is None:
                lane.extruder_obj.tool_start_state = head

        for lane in self.lanes.values():
            now = self._present(st, lane)
            seen = self._seen.get(lane.name)
            if seen is None:
                self._seen[lane.name] = now
                self._set_presence(lane, now)
                continue
            if now == seen:
                self._pending.pop(lane.name, None)
                continue
            value, count = self._pending.get(lane.name, (now, 0))
            count = count + 1 if value == now else 1
            if count < self.stable_polls:
                self._pending[lane.name] = (now, count)
                continue
            self._pending.pop(lane.name, None)
            self._seen[lane.name] = now
            self._set_presence(lane, now)
            if lane.status == AFCLaneState.EJECTING and not now:
                lane.status = AFCLaneState.NONE
            try:
                # AFC's own insert/runout logic: set_loaded, spool values,
                # infinite spool or pause on runout
                lane.handle_load_runout(eventtime, now)
            except Exception:
                self.logger.error("AFC_IFS: %s sensor change failed\n%s"
                                  % (lane.name, traceback.format_exc()))

        self._sync_tool(st.get('channel') or 0)
        return eventtime + self.poll_interval

    def _on_ifs_loaded(self, slot):
        # Kiln's "ifs:loaded" fires after every load, unload, swap and
        # IFS_SET_CHANNEL; ours are accounted for by AFC itself.
        if self._busy:
            return
        self.reactor.register_callback(lambda e: self._sync_tool(slot or 0))

    def _sync_tool(self, slot):
        """Make AFC's toolhead lane match Kiln's ifs.channel."""
        if self._busy or self.afc.current_state not in ('Idle', None):
            return
        want = self._lane_for_slot(slot) if slot else None
        have = None
        for lane in self.lanes.values():
            if lane.tool_loaded or (lane.extruder_obj is not None
                                    and lane.extruder_obj.lane_loaded == lane.name):
                have = lane
                break
        if want is have:
            return
        if have is not None:
            self.logger.info("AFC_IFS: %s left the toolhead outside AFC" % have.name)
            have.set_tool_unloaded(normal_toolchange=True)
            if have.prep_state:
                have.status = AFCLaneState.LOADED
        if want is not None:
            self.logger.info("AFC_IFS: %s is in the toolhead (Kiln ifs.channel %d)"
                             % (want.name, slot))
            want.loaded_to_hub = True
            want.set_tool_loaded(normal_toolchange=True)
        self.afc.save_vars()

    # -- PREP ----------------------------------------------------------------

    def system_Test(self, cur_lane, delay, assignTcmd, enable_movement):
        st = self._ifs_status()
        succeeded = bool(st.get('connected'))
        msg = ''
        if not succeeded:
            msg = '<span class=error--text>IFS NOT CONNECTED</span>'
        else:
            present = self._present(st, cur_lane)
            self._seen[cur_lane.name] = present
            self._set_presence(cur_lane, present)
            in_head = (st.get('channel') or 0) == self._slot[cur_lane.name]
            if in_head and not cur_lane.tool_loaded:
                cur_lane.set_tool_loaded()
            elif cur_lane.tool_loaded and not in_head:
                cur_lane.set_tool_unloaded()
            if present or in_head:
                self.lane_loaded(cur_lane)
                cur_lane.status = AFCLaneState.LOADED
                msg += '<span class=success--text>LOCKED AND LOADED</span>'
                if in_head:
                    cur_lane.loaded_to_hub = True
                    cur_lane.status = AFCLaneState.TOOLED
                    self.afc.spool.set_active_spool(cur_lane.spool_id)
                    self.lane_tool_loaded(cur_lane)
                    msg += '<span class=primary--text> in ToolHead</span>'
                    self.printer.send_event("afc:tool_loaded", cur_lane)
            else:
                if not cur_lane.remember_spool:
                    self.afc.spool.clear_values(cur_lane)
                self.lane_not_ready(cur_lane)
                msg += 'EMPTY READY FOR SPOOL'
        if assignTcmd:
            self.afc.function.TcmdAssign(cur_lane)
        cur_lane.do_enable(False)
        self.logger.info('{lane_name} tool cmd: {tcmd:3} {msg}'.format(
            lane_name=cur_lane.name, tcmd=cur_lane.map, msg=msg))
        cur_lane.set_afc_prep_done()
        return succeeded

    # -- moves (all through Kiln) --------------------------------------------

    def _run(self, template, lane):
        cmd = template.format(slot=self._slot[lane.name], lane=lane.name)
        self.logger.debug("AFC_IFS: " + cmd)
        self._busy = True
        try:
            self.gcode.run_script_from_command(cmd)
            return True, ''
        except Exception as e:
            return False, str(e)
        finally:
            self._busy = False
            # Re-read slots from scratch after our move
            self._pending.clear()

    def unit_load_lane(self, cur_lane, cur_extruder):
        swap = self._swap_from is not None
        self._swap_from = None
        ok, err = self._run(self.swap_cmd if swap else self.load_cmd, cur_lane)
        st = self._ifs_status()
        if ok and (st.get('channel') or 0) != self._slot[cur_lane.name]:
            ok, err = False, "Kiln reports slot %s in the toolhead" % st.get('channel')
        if not ok:
            self.afc.error.handle_lane_failure(
                cur_lane, "IFS load of %s failed: %s" % (cur_lane.name, err),
                pause=self.afc.function.in_print())
            self._sync_tool(st.get('channel') or 0)
            return False
        cur_lane.loaded_to_hub = True
        cur_lane.status = AFCLaneState.TOOL_LOADED
        self.afc.save_vars()
        return True

    def unit_unload_lane(self, cur_lane, cur_extruder):
        nxt = self.afc.next_lane_load
        if (self.use_swap and nxt in self._slot and nxt != cur_lane.name
                and self.afc.function.in_print()):
            # TOOL_LOAD of nxt follows at once: let IFS_SWAP do both
            self._swap_from = cur_lane.name
            ok = True
        else:
            self._swap_from = None
            ok, err = self._run(self.unload_cmd, cur_lane)
            st = self._ifs_status()
            if ok and (st.get('channel') or 0) == self._slot[cur_lane.name]:
                ok, err = False, "the lane is still in the toolhead"
            if not ok:
                self.afc.error.handle_lane_failure(
                    cur_lane, "IFS unload of %s failed: %s" % (cur_lane.name, err),
                    pause=self.afc.function.in_print())
                return False
        if self.afc.post_unload_macro is not None:
            self.gcode.run_script_from_command(self.afc.post_unload_macro)
        cur_lane.set_tool_unloaded(normal_toolchange=True)
        cur_lane.status = AFCLaneState.LOADED if cur_lane.prep_state else AFCLaneState.NONE
        self.afc.save_vars()
        return True

    def eject_lane(self, lane):
        ok, err = self._run(self.eject_cmd, lane)
        if not ok:
            self.afc.error.AFC_error("IFS eject of %s failed: %s" % (lane.name, err),
                                     pause=False)

    def lane_unload(self, cur_lane):
        self.eject_lane(cur_lane)
        return True

    # -- not applicable to the IFS -------------------------------------------

    def prep_load(self, lane):
        pass

    def prep_post_load(self, lane):
        pass

    def lane_move(self, cur_lane, distance, speed_mode):
        self.logger.info("LANE_MOVE is not available on IFS lanes; use IFS_* commands")

    def get_lane_reset_command(self, lane, dis):
        return None

    def calibration_lane_message(self):
        return "The IFS needs no AFC calibration (Kiln: IFS_TUNE)."

    def calibrate_lane(self, cur_lane, tol):
        return False, self.calibration_lane_message(), 0

    def calibrate_bowden(self, cur_lane, dis, tol):
        return False, self.calibration_lane_message(), 0

    def calibrate_hub(self, cur_lane, tol):
        return False, self.calibration_lane_message(), 0

    def calibrate_td1(self, cur_lane, dis, tol):
        return False, "TD-1 is not supported on IFS lanes", 0

    def select_lane(self, lane, sel_prep=False):
        return True, 0

    def return_to_home(self, prep=False):
        return True

    def check_runout(self, cur_lane):
        # AFC's runout path only for the lane in the toolhead while printing
        return (cur_lane.name == self.afc.function.get_current_lane()
                and self.afc.function.is_printing()
                and cur_lane.status not in (AFCLaneState.EJECTING,
                                            AFCLaneState.CALIBRATING))


def load_config_prefix(config):
    return afcIFS(config)
