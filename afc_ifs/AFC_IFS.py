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

import re
import traceback
from configparser import Error as error

try: from extras.AFC_utils import ERROR_STR
except: raise error("Error when trying to import AFC_utils.ERROR_STR\n{trace}".format(trace=traceback.format_exc()))

try: from extras.AFC_lane import AFCLaneState
except: raise error(ERROR_STR.format(import_lib="AFC_lane", trace=traceback.format_exc()))

try: from extras.AFC_unit import afcUnit
except: raise error(ERROR_STR.format(import_lib="AFC_unit", trace=traceback.format_exc()))


# Material names Kiln's IFS_SET_MATERIAL accepts (its parse_material)
MATERIAL_RE = re.compile(r"^[A-Za-z0-9+_.-]{1,16}$")
HEX_RE = re.compile(r"^#?([0-9A-Fa-f]{6})")


def color_hex(value):
    """'#RRGGBB' (upper case) from AFC's or Kiln's color, else ''."""
    m = HEX_RE.match(str(value or '').strip())
    return '#' + m.group(1).upper() if m else ''


# Seconds AFC gets to fetch a spool from Spoolman after SET_SPOOL_ID
SPOOL_WAIT = 10.0


def spool_id(value):
    """A Spoolman spool ID (int > 0) from AFC's or Kiln's value, else None."""
    try:
        sid = int(value)
    except (TypeError, ValueError):
        return None
    return sid if sid > 0 else None


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
        # Keep AFC's lane material/color and Kiln's ifs_materials in step
        self.sync_materials = config.getboolean('sync_materials', True)
        self.materials_name = config.get('materials', 'ifs_materials')
        self.materials = None
        self._synced = {}         # slot -> (type, '#RRGGBB') both sides agree on
        # Keep AFC's lane spools and Kiln's spoolman plugin (spoolman_slots,
        # which makes a slot's spool active on every IFS load) in step, so
        # both set the same active spool
        self.sync_spools = config.getboolean('sync_spools', True)
        self.spool_slots_name = config.get('spool_slots', 'spoolman_slots')
        self.spool_slots = None
        self._spool_synced = {}   # slot -> spool ID (or None) both sides agree on
        self._spool_wait = {}     # slot -> (spool ID, deadline) while AFC fetches it
        self._spool_pushed = {}   # slot -> deadline while our SPOOLMAN_SET_SLOT runs
        self.load_cmd = config.get('load_cmd', 'IFS_LOAD SLOT={slot}')
        self.unload_cmd = config.get('unload_cmd', 'IFS_UNLOAD SLOT={slot}')
        self.swap_cmd = config.get('swap_cmd', 'IFS_SWAP CHANNEL={slot}')
        self.eject_cmd = config.get('eject_cmd', 'IFS_EJECT SLOT={slot}')
        # Route T<n> to the lane of the slot Kiln maps tool n to (ifs.tool_map:
        # IFS_MAP_MODE, the screen, IFS_MAP_TOOL, a print's color matching)
        self.follow_tool_map = config.getboolean('follow_tool_map', True)
        self._tool_map = None     # Kiln's tool_map we last applied

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
        self.materials = self.printer.lookup_object(self.materials_name, None)
        self.spool_slots = self.printer.lookup_object(self.spool_slots_name, None)
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
            self._poll_safe, self.reactor.monotonic() + self.poll_interval)

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

    def _poll_safe(self, eventtime):
        # An exception in a reactor timer shuts Klipper down; log it instead
        try:
            return self._poll(eventtime)
        except Exception:
            self.logger.error("AFC_IFS: poll failed\n%s" % traceback.format_exc())
            return eventtime + max(self.poll_interval, 5.0)

    def _poll(self, eventtime):
        if self._busy or self.ifs is None:
            return eventtime + self.poll_interval
        st = self._ifs_status(eventtime)
        if not st or not self._prepped():
            # AFC connects to Moonraker and reads the lanes at PREP;
            # system_Test does the first presence, tool and material pass
            return eventtime + self.poll_interval
        if self.follow_tool_map:
            self._sync_tool_map(st.get('tool_map'))
        if st.get('busy'):
            return eventtime + self.poll_interval

        head =self._head_detected(eventtime)
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
        if self.sync_materials:
            self._sync_materials(eventtime)
        if self.sync_spools:
            self._sync_spools(eventtime)
        return eventtime + self.poll_interval

    def _prepped(self):
        return bool(self.lanes) and all(
            getattr(lane, '_afc_prep_done', False) for lane in self.lanes.values())

    def _send_lane_data(self, lane):
        # AFC's Moonraker client exists only once PREP has started
        if getattr(self.afc, 'moonraker', None) is not None:
            lane.send_lane_data()

    # -- tool map: Kiln's ifs.tool_map -> AFC's tool_cmds -----------------------

    def _sync_tool_map(self, tool_map):
        """Kiln maps tools to slots itself (color matching at print start, the
        screen, IFS_MAP_TOOL) and may give two tools one slot. Each change is
        replayed into AFC with SET_MAP, which with enable_multiple_mapping
        moves one T(n) to a lane (AFC DEV). Applied when Kiln's map changes; a
        SET_MAP in between holds until then."""
        if not isinstance(tool_map, dict) or tool_map == self._tool_map:
            return
        self._tool_map = dict(tool_map)
        if not getattr(self.afc, 'enable_multiple_mapping', False):
            self.logger.info("AFC_IFS: follow_tool_map needs [AFC] enable_multiple_mapping: True")
            return
        moves = []
        for tool, slot in sorted(tool_map.items()):
            tcmd = "T%s" % (tool,)
            lane = self._lane_for_slot(slot)
            if (lane is None
                or tcmd not in self.afc.tool_cmds
                or self.afc.tool_cmds[tcmd] == lane.name):
                continue
            moves.append("SET_MAP LANE=%s MAP=%s" % (lane.name, tcmd))
        if not moves:
            return
        self.logger.info("AFC_IFS: following Kiln's tool map: %s" % ", ".join(moves))
        for cmd in moves:
            try:
                self.gcode.run_script(cmd)
            except Exception as e:
                self.logger.info("AFC_IFS: %s failed: %s" % (cmd, e))

    # -- material and color: AFC lanes <-> Kiln's ifs_materials ---------------

    def _kiln_slots(self, eventtime):
        if self.materials is None:
            return {}
        try:
            return self.materials.get_status(eventtime).get('slots') or {}
        except Exception:
            return {}

    def _apply_kiln(self, lane, slot):
        mtype = slot.get('type') or ''
        color = color_hex(slot.get('color'))
        lane.material = mtype
        if color:
            lane.color = color
        temp = slot.get('temp')
        if temp:
            lane.extruder_temp = temp
        if lane.spool_id is None and not lane.weight:
            # AFC's own default for a spool loaded without Spoolman
            # (AFC_spool._set_values, skipped once a color is set). The
            # panels draw a 0 g spool empty, without its color.
            lane.weight = 1000
        self._send_lane_data(lane)

    def _push_kiln(self, lane, mtype, color):
        args = ["SLOT=%d" % self._slot[lane.name]]
        if mtype and MATERIAL_RE.match(mtype):
            args.append("TYPE=%s" % mtype)
        if color:
            # Bare hex: Klipper takes '#' as the start of a comment
            args.append("COLOR=%s" % color[1:])
        if len(args) == 1:
            return
        if lane.extruder_temp:
            args.append("TEMP=%d" % int(float(lane.extruder_temp)))
        script = "IFS_SET_MATERIAL " + " ".join(args)
        self.logger.debug("AFC_IFS: " + script)

        def run(eventtime):
            try:
                self.gcode.run_script(script)
            except Exception as e:
                self.logger.info("AFC_IFS: %s failed: %s" % (script, e))
        self.reactor.register_callback(run)

    def _sync_materials(self, eventtime):
        slots = self._kiln_slots(eventtime)
        if not slots:
            return
        changed = False
        for lane in self.lanes.values():
            slot = self._slot[lane.name]
            k = slots.get(str(slot))
            if k is None or not lane.prep_state:
                # An empty lane: AFC may clear its values, Kiln keeps the
                # slot's last material; neither is news for the other side
                self._synced.pop(slot, None)
                continue
            kiln = (k.get('type') or '', color_hex(k.get('color')))
            afc = (lane.material or '', color_hex(lane.color))
            agreed = self._synced.get(slot)
            if agreed is None or kiln != agreed:
                # First look (Kiln wins), or set on Kiln's side: screen,
                # IFS_SET_MATERIAL, FF_NFC, ...
                if afc != kiln:
                    self._apply_kiln(lane, k)
                    changed = True
                self._synced[slot] = kiln
            elif afc != agreed:
                # Set on AFC's side: SET_COLOR, SET_MATERIAL, SET_SPOOL_ID
                self._push_kiln(lane, afc[0], afc[1])
                self._synced[slot] = afc
        if changed:
            self.afc.save_vars()

    # -- spools: AFC lanes <-> Kiln's spoolman plugin (spoolman_slots) --------

    def _run_later(self, *scripts):
        def run(eventtime):
            for script in scripts:
                self.logger.debug("AFC_IFS: " + script)
                try:
                    self.gcode.run_script(script)
                except Exception as e:
                    self.logger.info("AFC_IFS: %s failed: %s" % (script, e))
        self.reactor.register_callback(run)

    def _sync_spools(self, eventtime):
        """Both sides keep a spool per slot and set Moonraker's active spool
        on a load: AFC on its tool loads, the plugin on every Kiln
        ifs:loaded. AFC wins on the first look; after that a change on
        either side is copied to the other."""
        if self.spool_slots is None or getattr(self.afc, 'spoolman', None) is None:
            return
        try:
            kiln_slots = self.spool_slots.get_status(eventtime).get('slots') or {}
        except Exception:
            return
        for lane in self.lanes.values():
            slot = self._slot[lane.name]
            if str(slot) not in kiln_slots:
                continue
            kiln = spool_id(kiln_slots.get(str(slot)))
            afc = spool_id(lane.spool_id)
            wait = self._spool_wait.get(slot)
            if wait is not None:
                # AFC fetches a spool from Spoolman after SET_SPOOL_ID returns
                sid, deadline = wait
                if afc != sid and eventtime < deadline:
                    continue
                del self._spool_wait[slot]
                if afc != sid:
                    self.logger.info("AFC_IFS: AFC did not take spool %s for %s; Kiln's"
                                     " spoolman slot %d follows AFC" % (sid, lane.name, slot))
                self._spool_synced[slot] = sid
            pushed = self._spool_pushed.get(slot)
            if pushed is not None:
                # Our SPOOLMAN_SET_SLOT may not have run yet
                if kiln != self._spool_synced.get(slot) and eventtime < pushed:
                    continue
                del self._spool_pushed[slot]
            if slot not in self._spool_synced:
                self._spool_synced[slot] = afc
                if kiln != afc:
                    self._push_spool(slot, afc, eventtime)
            elif kiln != self._spool_synced[slot]:
                # Set on Kiln's side: SPOOLMAN_SET_SLOT, the screen, ...
                self._spool_synced[slot] = kiln
                if afc != kiln:
                    self._apply_spool(lane, slot, kiln, eventtime)
            elif afc != self._spool_synced[slot]:
                # Set on AFC's side: SET_SPOOL_ID, the AFC panels, FF_NFC, runout
                self._spool_synced[slot] = afc
                self._push_spool(slot, afc, eventtime)

    def _push_spool(self, slot, sid, eventtime):
        self._spool_pushed[slot] = eventtime + SPOOL_WAIT
        self._run_later("SPOOLMAN_SET_SLOT SLOT=%d SPOOL=%s" % (slot, sid or ''))

    def _apply_spool(self, lane, slot, sid, eventtime):
        scripts = []
        if sid is not None:
            # AFC refuses a spool another lane still has: it moved
            for other in self.afc.lanes.values():
                if other is not lane and spool_id(other.spool_id) == sid:
                    scripts.append("SET_SPOOL_ID LANE=%s SPOOL_ID=" % other.name)
        scripts.append("SET_SPOOL_ID LANE=%s SPOOL_ID=%s" % (lane.name, sid or ''))
        self._spool_wait[slot] = (sid, eventtime + SPOOL_WAIT)
        self._run_later(*scripts)

    def _on_ifs_loaded(self, slot):
        # Kiln's "ifs:loaded" fires after every load, unload, swap and
        # IFS_SET_CHANNEL; ours are accounted for by AFC itself.
        if self._busy or not self._prepped():
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
        if isinstance(cur_lane.map, tuple):
            # AFC_lane keeps a config `map:` as getlist()'s tuple; SET_MAP with
            # enable_multiple_mapping then fails on tuple + list (AFC DEV)
            cur_lane.map = list(cur_lane.map)
        if succeeded and self.sync_materials and cur_lane.prep_state:
            k = self._kiln_slots(self.reactor.monotonic()).get(
                str(self._slot[cur_lane.name]))
            if k is not None:
                self._apply_kiln(cur_lane, k)
                self._synced[self._slot[cur_lane.name]] = (
                    k.get('type') or '', color_hex(k.get('color')))
        # AFC cleared lane_data when PREP started; write this lane back
        self._send_lane_data(cur_lane)
        cur_lane.do_enable(False)
        self.logger.info('{lane_name} tool cmd: {tcmd:3} {msg}'.format(
            lane_name=cur_lane.name, tcmd=cur_lane.map_to_string(), msg=msg))
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

    def _fail_state(self):
        # Outside a print handle_lane_failure doesn't pause, and then nothing
        # takes AFC out of Loading/Unloading. Set the error state as AFC's
        # own failures do (pause_print); RESET_FAILURE clears it.
        if not self.afc.error_state:
            self.afc.error.set_error_state(True)

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
            self._fail_state()
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
                self._fail_state()
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
