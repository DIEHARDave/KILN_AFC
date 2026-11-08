"""KILN_AFC against Kiln's ifs.py and a simulated IFS, in Klipper batch mode.

Runs Klipper (Kiln's pinned version, linux-process MCU dictionary, no
hardware) with Kiln's ifs.py (tests/kiln_files, copied from a printer),
the plugin's bootstrap and AFC_IFS unit, the real AFC-Klipper-Add-On, a
fake IFS on a pty (KILN_IFSJacker's fake_ifs.py) and a Moonraker stub, and
drives AFC's commands through kiln_test_probe.py.

    python3 tests/test_kiln_afc.py --klipper ~/kiln-klipper \\
        --afc /mnt/d/Dev/3D-Printing/AFC-Klipper-Add-On \\
        --emu /mnt/d/Dev/3D-Printing/KILN_IFSJacker/emulator \\
        --python ~/klippy-test-env/bin/python

Linux only (ptys, FIFOs).
"""
import argparse
import http.server
import json
import os
import shutil
import subprocess
import sys
import threading
import time

HERE = os.path.dirname(os.path.realpath(__file__))
PLUGIN = os.path.dirname(HERE)
WORK = os.path.expanduser("~/kiln_afc_test")
LINK = "/tmp/ifs_afc"
MOONRAKER_PORT = 7139

results = []

def check(name, ok, detail=""):
    results.append((name, bool(ok), detail))
    print("%s  %s%s" % ("PASS" if ok else "FAIL", name,
                        "  (%s)" % (detail,) if detail else ""), flush=True)
    return ok

# -- Moonraker stub: what AFC asks for ---------------------------------------

class MoonrakerStub(http.server.BaseHTTPRequestHandler):
    db = {}
    def log_message(self, *a):
        pass
    def _send(self, result):
        body = json.dumps({'result': result}).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)
    def do_GET(self):
        p = self.path
        if p.startswith('/server/info'):
            return self._send({'klippy_state': 'ready'})
        if p.startswith('/server/config'):
            return self._send({'orig': {}, 'config': {}})
        if p.startswith('/server/database/item'):
            return self._send({'value': {}})
        if p.startswith('/server/files/metadata'):
            return self._send({})
        self._send({})
    def do_POST(self):
        n = int(self.headers.get('Content-Length') or 0)
        body = self.rfile.read(n)
        try:
            item = json.loads(body)
            if item.get('namespace') == 'lane_data':
                MoonrakerStub.db[item['key']] = item['value']
        except ValueError:
            pass
        self._send({})
    def do_DELETE(self):
        self._send({})

def start_moonraker():
    srv = http.server.ThreadingHTTPServer(('127.0.0.1', MOONRAKER_PORT), MoonrakerStub)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv

# -- Klipper in batch mode, driven through kiln_test_probe.py ---------------

class Api:
    def __init__(self, writer):
        self.writer = writer
        self.next_id = 0
        self.results = os.path.join(WORK, "results.jsonl")
        self.status = os.path.join(WORK, "status.json")
    def _result(self, rid, timeout):
        end = time.time() + timeout
        while time.time() < end:
            if os.path.exists(self.results):
                for line in open(self.results).read().splitlines():
                    r = json.loads(line)
                    if r['id'] == rid:
                        return r
            time.sleep(.05)
        raise TimeoutError("no result for TEST_RUN %s" % (rid,))
    def gcode(self, script, timeout=180.):
        out = []
        for line in [l for l in script.split("\n") if l.strip()]:
            self.next_id += 1
            rid = str(self.next_id)
            os.write(self.writer, ('TEST_RUN ID=%s CMD="%s"\n' % (
                rid, line.replace('"', "'"))).encode())
            r = self._result(rid, timeout)
            out += r['output']
            if not r['ok']:
                return False, r['error'], out
        time.sleep(.3)
        return True, "", out
    def query(self, *objects):
        for i in range(40):
            try:
                st = json.load(open(self.status))['status']
                break
            except (OSError, ValueError):
                time.sleep(.05)
        else:
            st = {}
        return {o: st.get(o) for o in objects}

LANES = ["AFC_lane lane%d" % i for i in range(1, 5)]
PROBE_OBJECTS = ", ".join(["ifs", "ifs_materials", "AFC", "AFC_IFS IFS", "print_stats",
                           "AFC_extruder extruder"] + LANES)

TEST_CFG = """
[filament_switch_sensor toolhead]
switch_pin: gpio20
pause_on_runout: False
"""

def plugin_cfg(args):
    text = open(os.path.join(PLUGIN, "plugin.cfg")).read()
    text = text.replace("/home/klipper/printer_data/config/AFC",
                        os.path.join(WORK, "AFC"))
    text = text.replace("[kiln_afc]\n", "[kiln_afc]\nafc_path: %s\n" % args.afc)
    text = text.replace("force_assign_map: True\n",
                        "force_assign_map: True\nmoonraker_port: %d\n"
                        "moonraker_host: http://127.0.0.1\n" % MOONRAKER_PORT)
    text = text.replace("extruder: extruder\n",
                        "extruder: extruder\nhead_sensor: filament_switch_sensor toolhead\n", 1)
    return text

class Klippy:
    def __init__(self, args):
        base = open(os.path.join(args.emu, "klipper_test", "printer.cfg")).read()
        base = base.replace("channels: auto\n", "").replace("/tmp/kiln_ifs_test", WORK).replace("/tmp/ifs\n", LINK + "\n")
        cfg = base + TEST_CFG + plugin_cfg(args)
        cfg += "\n[kiln_test_probe]\ndir: %s\nobjects: %s\n" % (WORK, PROBE_OBJECTS)
        self.cfg = os.path.join(WORK, "printer.cfg")
        open(self.cfg, "w").write(cfg)
        self.fifo = os.path.join(WORK, "input.fifo")
        os.mkfifo(self.fifo)
        self.log = os.path.join(WORK, "klippy.log")
        k = os.path.expanduser(args.klipper)
        self.writer = os.open(self.fifo, os.O_RDWR)
        self.proc = subprocess.Popen(
            [args.python, os.path.join(k, "klippy", "klippy.py"), self.cfg,
             "-i", self.fifo, "-o", os.path.join(WORK, "out.serial"),
             "-d", os.path.join(k, "out", "klipper.dict"), "-l", self.log],
            stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
        self.api = Api(self.writer)
        status = os.path.join(WORK, "status.json")
        for i in range(200):
            if os.path.exists(status) or self.proc.poll() is not None:
                break
            time.sleep(.1)
        self.ready = os.path.exists(status)
    def stop(self):
        try:
            os.close(self.writer)
        except OSError:
            pass
        try:
            self.proc.wait(10)
        except subprocess.TimeoutExpired:
            self.proc.kill()

def wait_for(fn, timeout, step=.25):
    end = time.time() + timeout
    while time.time() < end:
        v = fn()
        if v:
            return v
        time.sleep(step)
    return fn()

def lane(api, n):
    return api.query("AFC_lane lane%d" % n)["AFC_lane lane%d" % n] or {}

def ifs(api):
    return api.query("ifs")["ifs"] or {}

# AFC's status: current_load is the lane in the toolhead, current_lane the
# lane a load is under way for
def afc(api):
    return api.query("AFC")["AFC"] or {}

def run(args):
    emu = subprocess.Popen(
        [sys.executable, "-u", os.path.join(args.emu, "fake_ifs.py"),
         "--link", LINK, "--speed", "40", "--log", os.path.join(WORK, "ifs_cmds.log")],
        cwd=args.emu, stdout=open(os.path.join(WORK, "fake_ifs.log"), "w"),
        stderr=subprocess.STDOUT)
    for i in range(50):
        if os.path.exists(LINK):
            break
        time.sleep(.1)
    time.sleep(.5)
    k = Klippy(args)
    api = k.api
    try:
        if not check("klipper ready with AFC + AFC_IFS", k.ready,
                     "see %s" % k.log):
            return
        st = api.query("AFC")["AFC"]
        check("AFC object exists", st is not None and '_error' not in st,
              st and st.get('_error'))
        api.gcode("SET_KINEMATIC_POSITION X=100 Y=100 Z=10")
        wait_for(lambda: ifs(api).get('connected'), 15)
        # PREP runs 2 s after ready and waits for Moonraker
        prepped = wait_for(lambda: all(lane(api, n).get('status') == 'Loaded'
                                       for n in range(1, 5)), 60)
        check("PREP: lanes 1-4 Loaded from ifs.loaded_channels", prepped,
              [lane(api, n).get('status') for n in range(1, 5)])
        check("lane maps T0-T3", [lane(api, n).get('map') for n in range(1, 5)]
              == ["T0", "T1", "T2", "T3"],
              [lane(api, n).get('map') for n in range(1, 5)])

        check("PREP: lane_data written for lane1-4",
              wait_for(lambda: sorted(MoonrakerStub.db) == ["lane1", "lane2", "lane3", "lane4"], 10),
              sorted(MoonrakerStub.db))

        def kslot(n):
            return (api.query("ifs_materials")["ifs_materials"] or {}).get('slots', {}).get(str(n), {})
        ok, err, out = api.gcode("IFS_SET_MATERIAL SLOT=2 TYPE=PETG COLOR=FF0000")
        got = wait_for(lambda: (lane(api, 2).get('material'), lane(api, 2).get('color'))
                       == ('PETG', '#FF0000'), 10)
        check("Kiln -> AFC: IFS_SET_MATERIAL shows on lane2", ok and got,
              err or (lane(api, 2).get('material'), lane(api, 2).get('color')))
        check("Kiln -> AFC: lane_data lane2 updated",
              wait_for(lambda: MoonrakerStub.db.get('lane2', {}).get('material') == 'PETG', 10),
              MoonrakerStub.db.get('lane2'))
        ok, err, out = api.gcode("SET_COLOR LANE=lane3 COLOR=00FF00\n"
                                 "SET_MATERIAL LANE=lane3 MATERIAL=ABS")
        got = wait_for(lambda: (kslot(3).get('type'), kslot(3).get('color')) == ('ABS', '#00FF00'), 10)
        check("AFC -> Kiln: SET_COLOR/SET_MATERIAL reach ifs_materials slot 3", ok and got,
              err or kslot(3))

        ok, err, out = api.gcode("T1")
        check("T1 loads lane2 through IFS_LOAD", ok and ifs(api).get('channel') == 2,
              err or ifs(api).get('channel'))
        check("AFC: lane2 in the toolhead", afc(api).get('current_load') == 'lane2'
              and lane(api, 2).get('tool_loaded'),
              (afc(api).get('current_load'), lane(api, 2).get('status')))

        ok, err, out = api.gcode("T2")
        check("T2 (idle): unload lane2, load lane3", ok and ifs(api).get('channel') == 3,
              err or ifs(api).get('channel'))
        check("AFC: lane3 current, lane2 back to Loaded",
              afc(api).get('current_load') == 'lane3' and lane(api, 2).get('status') == 'Loaded',
              (afc(api).get('current_load'), lane(api, 2).get('status')))

        ok, err, out = api.gcode("TOOL_UNLOAD")
        check("TOOL_UNLOAD empties the toolhead", ok and ifs(api).get('channel') == 0,
              err or ifs(api).get('channel'))
        check("AFC: nothing current", afc(api).get('current_load') is None,
              afc(api).get('current_load'))

        # Kiln's tool map (IFS_MAP_TOOL, a print's color matching) routes T<n>
        ok, err, out = api.gcode("IFS_MAP_TOOL TOOL=1 SLOT=4")
        time.sleep(3)
        ok2, err2, out = api.gcode("T1")
        check("T1 follows Kiln's tool map (tool 1 -> slot 4)",
              ok and ok2 and ifs(api).get('channel') == 4
              and afc(api).get('current_load') == 'lane4',
              err or err2 or (ifs(api).get('channel'), afc(api).get('current_load')))
        api.gcode("IFS_MAP_TOOL RESET=1")
        time.sleep(3)
        ok, err, out = api.gcode("T1")
        check("T1 back on lane2 after IFS_MAP_TOOL RESET",
              ok and ifs(api).get('channel') == 2, err or ifs(api).get('channel'))
        check("lane maps back to T0-T3 after RESET",
              [lane(api, n).get('map') for n in (1, 2, 3, 4)] == ["T0", "T1", "T2", "T3"],
              [lane(api, n).get('map') for n in (1, 2, 3, 4)])
        api.gcode("TOOL_UNLOAD")

        ok, err, out = api.gcode("IFS_LOAD SLOT=4")
        mirrored = wait_for(lambda: afc(api).get('current_load') == 'lane4', 10)
        check("IFS_LOAD by hand is mirrored into AFC (lane4)", ok and mirrored,
              err or afc(api).get('current_load'))
        ok, err, out = api.gcode("IFS_UNLOAD")
        cleared = wait_for(lambda: afc(api).get('current_load') is None, 10)
        check("IFS_UNLOAD by hand is mirrored (nothing current)", ok and cleared,
              err or afc(api).get('current_load'))

        ok, err, out = api.gcode("LANE_UNLOAD LANE=lane1")
        empty = wait_for(lambda: 1 not in (ifs(api).get('loaded_channels') or [1]), 15)
        check("LANE_UNLOAD (eject) lane1 through IFS_EJECT", ok and empty,
              err or ifs(api).get('loaded_channels'))
        gone = wait_for(lambda: lane(api, 1).get('status') == 'None', 15)
        check("AFC: lane1 empty after eject", gone, lane(api, 1).get('status'))

        # A print with tool changes: the first load is IFS_LOAD, each change
        # one IFS_SWAP (AFC_IFS folds the unload into it)
        open(os.path.join(WORK, "gcodes", "swap.gcode"), "w").write(
            "G90\nT1\nG1 X20 Y20 F6000\nT2\nG1 X30 Y30\nT3\nG1 X40 Y40\n")
        afc_log = os.path.join(WORK, "AFC.log")
        mark = os.path.getsize(afc_log)
        ok, err, out = api.gcode("SDCARD_PRINT_FILE FILENAME=swap.gcode")
        done = wait_for(lambda: api.query("print_stats")["print_stats"]["state"]
                        in ("complete", "error", "cancelled"), 240, step=.5)
        state = api.query("print_stats")["print_stats"]
        check("print with T1, T2, T3 completes", ok and done
              and state["state"] == "complete", err or state)
        cmds = [l.split("AFC_IFS: ", 1)[1].strip() for l in
                open(afc_log).read()[mark:].splitlines() if "AFC_IFS: IFS_" in l]
        check("print: IFS_LOAD then one IFS_SWAP per change", cmds ==
              ["IFS_LOAD SLOT=2", "IFS_SWAP CHANNEL=3", "IFS_SWAP CHANNEL=4"], cmds)
        check("print: lane4 in the toolhead at the end",
              ifs(api).get('channel') == 4 and afc(api).get('current_load') == 'lane4',
              (ifs(api).get('channel'), afc(api).get('current_load')))
        check("print: lane2 and lane3 back to Loaded",
              [lane(api, n).get('status') for n in (2, 3)] == ['Loaded', 'Loaded'],
              [lane(api, n).get('status') for n in (2, 3)])
    finally:
        k.stop()
        emu.terminate()
        emu.wait()
        afc_log = os.path.join(WORK, "AFC.log")
        print("logs: %s, %s" % (k.log, afc_log))

def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--klipper", required=True)
    ap.add_argument("--afc", required=True, help="AFC-Klipper-Add-On checkout")
    ap.add_argument("--emu", required=True, help="KILN_IFSJacker emulator dir")
    ap.add_argument("--python", default=sys.executable)
    args = ap.parse_args()
    args.afc = os.path.expanduser(args.afc)
    args.emu = os.path.expanduser(args.emu)
    extras = os.path.join(os.path.expanduser(args.klipper), "klippy", "extras")
    links = {"ifs.py": os.path.join(HERE, "kiln_files", "ifs.py"),
             "kiln_test_probe.py": os.path.join(args.emu, "klipper_test", "kiln_test_probe.py"),
             "kiln_afc.py": os.path.join(PLUGIN, "extras", "kiln_afc.py")}
    for name, target in links.items():
        path = os.path.join(extras, name)
        if os.path.lexists(path):
            os.unlink(path)
        os.symlink(target, path)
    shutil.rmtree(WORK, ignore_errors=True)
    os.makedirs(os.path.join(WORK, "gcodes"))
    os.makedirs(os.path.join(WORK, "AFC"))
    open(os.path.join(WORK, "variables.cfg"), "w").close()
    if os.path.lexists(LINK):
        os.unlink(LINK)
    start_moonraker()
    try:
        run(args)
    finally:
        for name in links:
            os.unlink(os.path.join(extras, name))
    failed = [r for r in results if not r[1]]
    print("\n%d checks, %d failed" % (len(results), len(failed)))
    sys.exit(1 if failed else 0)

if __name__ == "__main__":
    main()
