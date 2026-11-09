# KILN_AFC bootstrap: runs ArmoredTurtle's AFC-Klipper-Add-On on Kiln OS.
#
# Kiln's Klipper lives on a read-only squashfs, so AFC's modules cannot be
# linked into klippy/extras the way install-afc.sh does it. Kiln's plugin
# loader (ad5x_plugins) only takes lowercase module names, and AFC asks
# Klipper for its own helper objects with printer.load_object(), which only
# looks in klippy/extras. This module bridges both:
#
# - extras.__path__ gets the plugin's afc_ifs/ folder and the AFC checkout's
#   extras/ folder, so "from extras.AFC_utils import ..." resolves;
# - printer.load_object is wrapped so a section or object named after a
#   module in those folders (AFC, AFC_lane lane1, AFC_spool, ...) is created
#   from there, the way Klipper's own load_object would;
# - AFC's hardcoded unit-type lists learn the "IFS" type of afc_ifs/AFC_IFS.py.
#
# ad5x_plugins creates [kiln_afc] while it loads plugins, which is before
# Klipper reaches the AFC sections, so the wrapper is in place in time.
#
# Copyright (C) 2026 DIEHARDave. GPLv3, like Klipper and AFC.

import ast
import configparser
import importlib
import json
import logging
import os
import re
import shutil
import subprocess
import sys

PLUGIN_DIR = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
DEFAULT_AFC_DIR = os.path.join(PLUGIN_DIR, 'AFC-Klipper-Add-On')
# The AFC commit this plugin is tested with; the plugin's own checkout
# (DEFAULT_AFC_DIR) is brought to it at each Klipper start, so a plugin
# update from Moonraker's update manager also updates AFC
AFC_COMMIT_FILE = os.path.join(PLUGIN_DIR, 'AFC_COMMIT')
AFC_REPO = 'https://github.com/ArmoredTurtle/AFC-Klipper-Add-On.git'
GIT_TIMEOUT = 300
UNIT_DIR = os.path.join(PLUGIN_DIR, 'afc_ifs')
IFS_TYPE = 'IFS'

# KILN_AFCLite (plugin "afclite") is replaced by this plugin. Both make the
# AFC object and CHANGE_TOOL, so they can't start together; Kiln's
# ad5x_plugins defaults (its [ad5x_plugins] options override them).
AFCLITE = 'afclite'
AFCLITE_SPOOL_PREFIX = 'kiln_afclite_spool_e'
KILN_PATHS = {
    'plugins_file': '/home/klipper/printer_data/config/plugins.cfg',
    'moonraker_file': '/home/klipper/printer_data/config/moonraker-plugins.conf',
    'user_dir': '/data/ad5x/plugins',
    'moonraker_restart_file': '/data/ad5x/kiln-update/restart-moonraker',
}
# ad5x_plugins' include line: [include <dir>/<name>/plugi[n].cfg]
INCLUDE_RE = re.compile(r"^\s*\[include\s+\S*/(?P<name>[A-Za-z0-9_.-]+)/"
                        r"plugi(?:n|\[n\])\.cfg\s*\]\s*$")
# AFC_IFS reads this next to AFC's VarFile: lane spools to keep at its
# first spool sync (afclite's, or Kiln's spoolman slots)
SEED_SUFFIX = '.afclite-spools.json'


def _git(args, cwd=None, timeout=GIT_TIMEOUT):
    env = dict(os.environ, GIT_TERMINAL_PROMPT='0')
    env.setdefault('HOME', '/home/klipper')
    try:
        r = subprocess.run(['git'] + args, cwd=cwd, env=env,
                           stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                           stderr=subprocess.STDOUT, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as e:
        return 1, str(e)
    return r.returncode, r.stdout.decode('utf-8', 'replace').strip()


def sync_afc_checkout(afc_dir, commit_file=AFC_COMMIT_FILE, repo=AFC_REPO):
    """Clone AFC into afc_dir if it isn't there and check out the commit in
    commit_file (fetching it when it's new). Returns what changed, or None.
    Raises RuntimeError when git fails."""
    with open(commit_file) as f:
        want = f.read().strip().lower()
    if not re.match(r'^[0-9a-f]{7,40}$', want):
        raise RuntimeError("%s holds no commit" % commit_file)
    if not os.path.isdir(os.path.join(afc_dir, '.git')):
        rc, out = _git(['clone', '--quiet', repo, afc_dir])
        if rc:
            raise RuntimeError("git clone %s failed: %s" % (repo, out[-300:]))
        before = None
    else:
        rc, before = _git(['rev-parse', 'HEAD'], cwd=afc_dir, timeout=30)
        if rc == 0 and before.lower().startswith(want):
            return None
    rc, out = _git(['checkout', '--quiet', want], cwd=afc_dir, timeout=60)
    if rc:
        # A commit newer than the checkout
        rc, out = _git(['fetch', '--quiet', 'origin'], cwd=afc_dir)
        if rc:
            raise RuntimeError("git fetch failed: %s" % (out[-300:],))
        rc, out = _git(['checkout', '--quiet', want], cwd=afc_dir, timeout=60)
        if rc:
            raise RuntimeError("git checkout %s failed: %s"
                               % (want[:7], out[-300:]))
    return "AFC %s -> %s" % ((before or 'new')[:7], want[:7])


def _write_atomic(path, text):
    tmp = path + '.tmp'
    with open(tmp, 'w') as f:
        f.write(text)
    os.replace(tmp, path)


def _afclite_spools(config):
    """slot -> spool ID afclite kept in save_variables (it uses Kiln's
    spoolman slots instead when that plugin is on; AFC_IFS reads those)."""
    if not config.has_section('save_variables'):
        return {}
    path = config.getsection('save_variables').get('filename', None,
                                                     note_valid=False)
    if not path:
        return {}
    vars_ = configparser.ConfigParser()
    try:
        vars_.read(os.path.expanduser(path))
        items = vars_.items('Variables') if vars_.has_section('Variables') else []
    except configparser.Error:
        return {}
    spools = {}
    for key, val in items:
        if not key.startswith(AFCLITE_SPOOL_PREFIX):
            continue
        try:
            slot = int(key[len(AFCLITE_SPOOL_PREFIX):]) + 1
            sid = int(ast.literal_eval(val) or 0)
        except (ValueError, SyntaxError, TypeError):
            continue
        if 1 <= slot <= 16 and sid > 0:
            spools[str(slot)] = sid
    return spools


def retire_afclite(config):
    """Disable and delete KILN_AFCLite. Returns True when it was enabled in
    this Klipper start (Klipper then has to restart without it)."""
    opts = dict(KILN_PATHS)
    if config.has_section('ad5x_plugins'):
        sect = config.getsection('ad5x_plugins')
        for key in opts:
            opts[key] = sect.get(key, opts[key], note_valid=False)
    pdir = os.path.join(opts['user_dir'], AFCLITE)
    try:
        with open(opts['plugins_file']) as f:
            lines = f.read().splitlines()
    except OSError:
        lines = []
    def is_afclite(line):
        m = INCLUDE_RE.match(line)
        return m is not None and m.group('name') == AFCLITE
    keep = [l for l in lines if not is_afclite(l)]
    enabled = len(keep) != len(lines)
    try:
        with open(os.path.join(pdir, 'plugin.json')) as f:
            installed = json.load(f).get('name') == AFCLITE
    except (OSError, ValueError, AttributeError):
        installed = False
    if not enabled and not installed:
        return False
    # Switching over from afclite: the lane spools it had, for AFC_IFS's
    # first spool sync (an afclite that was already off left AFC in charge)
    if enabled and config.has_section('AFC'):
        var = config.getsection('AFC').get('VarFile', None, note_valid=False)
        if var:
            try:
                os.makedirs(os.path.dirname(var), exist_ok=True)
                _write_atomic(var + SEED_SUFFIX,
                              json.dumps({'slots': _afclite_spools(config)}))
            except OSError as e:
                logging.warning("kiln_afc: can't write %s: %s",
                                var + SEED_SUFFIX, e)
    if enabled:
        _write_atomic(opts['plugins_file'], "\n".join(keep) + "\n")
    # Its Moonraker part (update_manager entry) goes too; Moonraker would
    # stop on an include of a file that is gone
    mr_changed = False
    try:
        with open(opts['moonraker_file']) as f:
            mr = f.read().splitlines()
        mine = '/%s/moonraker.conf' % AFCLITE
        mr_keep = [l for l in mr if not (l.strip().startswith('[include')
                                         and mine in l)]
        if mr_keep != mr:
            _write_atomic(opts['moonraker_file'], "\n".join(mr_keep) + "\n")
            mr_changed = True
    except OSError:
        pass
    if installed:
        if os.path.islink(pdir):
            os.unlink(pdir)
        else:
            shutil.rmtree(pdir)
    if mr_changed:
        try:
            with open(opts['moonraker_restart_file'], 'w') as f:
                f.write("plugins\n")
        except OSError as e:
            logging.info("kiln_afc: %s: %s", opts['moonraker_restart_file'], e)
    logging.info("kiln_afc: retired afclite (was %s%s)",
                 "enabled" if enabled else "disabled",
                 ", deleted %s" % pdir if installed else "")
    return enabled


def _add_paths(afc_dir):
    import extras
    dirs = [UNIT_DIR, os.path.join(afc_dir, 'extras')]
    for d in dirs:
        if d not in extras.__path__:
            extras.__path__.append(d)
    return dirs


def _forget_modules(dirs):
    """Drop AFC's modules (and AFC_IFS) that an earlier Klipper start in
    this process imported, so RESTART loads the files on disk: Klipper keeps
    imported modules across RESTART, and Kiln's Moonraker can't restart the
    Klipper service after an update (provider: none). Returns the names."""
    names = set()
    for d in dirs:
        try:
            names.update(fn[:-3] for fn in os.listdir(d) if fn.endswith('.py'))
        except OSError:
            pass
    return sorted(n for n in names
                  if sys.modules.pop('extras.' + n, None) is not None)


def _teach_unit_type():
    # AFC keeps per-type behavior in module-level lists; IFS lanes behave
    # like OpenAMS lanes (one sensor per lane, no lane stepper).
    lane_mod = importlib.import_module('extras.AFC_lane')
    unit_mod = importlib.import_module('extras.AFC_unit')
    for lst in (lane_mod.ONLY_LOAD_TYPES, lane_mod.EXCLUDE_TYPES,
                unit_mod.SENSORLESS_UNITS):
        if IFS_TYPE not in lst:
            lst.append(IFS_TYPE)


class KilnAFC:
    def __init__(self, config):
        self.printer = printer = config.get_printer()
        self._retire_afclite(config)
        afc_path = config.get('afc_path', None)
        self.afc_dir = afc_path or DEFAULT_AFC_DIR
        if afc_path is None:
            # The plugin's own checkout follows AFC_COMMIT (an afc_path of
            # your own is left as it is)
            try:
                note = sync_afc_checkout(self.afc_dir)
            except (OSError, RuntimeError) as e:
                raise config.error(
                    "kiln_afc: can't bring AFC to the commit in AFC_COMMIT: "
                    "%s. Check the network, then restart Klipper "
                    "(/etc/init.d/S61klipper restart)." % (e,))
            if note:
                logging.info("kiln_afc: %s", note)
        if not os.path.isfile(os.path.join(self.afc_dir, 'extras', 'AFC.py')):
            raise config.error(
                "kiln_afc: no AFC checkout at %s. Run install.sh in the "
                "plugin folder (it clones AFC-Klipper-Add-On)." % self.afc_dir)
        self.dirs = _add_paths(self.afc_dir)
        reloaded = _forget_modules(self.dirs)
        if reloaded:
            logging.info("kiln_afc: reloading %d AFC modules", len(reloaded))
        _teach_unit_type()
        self._prepare_varfile(config)
        self._orig_load_object = printer.load_object
        printer.load_object = self._load_object
        logging.info("kiln_afc: AFC modules from %s", ", ".join(self.dirs))

    def _retire_afclite(self, config):
        # ENABLE_PLUGIN NAME=afc with afclite still on: this start has
        # both, which can't work. Drop afclite, then restart without it.
        try:
            was_on = retire_afclite(config)
        except OSError as e:
            raise config.error(
                "kiln_afc: can't remove afclite (KILN_AFCLite): %s. Run "
                "DISABLE_PLUGIN NAME=afclite, then REMOVE_PLUGIN NAME=afclite"
                % (e,))
        if not was_on:
            return
        reactor = self.printer.get_reactor()
        reactor.register_callback(
            lambda e: self.printer.request_exit('restart'),
            reactor.monotonic() + 2.)
        raise config.error(
            "KILN_AFC replaces afclite (KILN_AFCLite): afclite is now "
            "disabled and deleted. Klipper restarts without it in a moment.")

    def _prepare_varfile(self, config):
        # AFC's PREP stops with "AFC.var.unit file not found" on a first
        # start; install-afc.sh would have created its folder and file.
        if not config.has_section('AFC'):
            return
        var = config.getsection('AFC').get('VarFile', None, note_valid=False)
        if not var:
            return
        try:
            os.makedirs(os.path.dirname(var), exist_ok=True)
            unit = var + '.unit'
            if not os.path.exists(unit):
                with open(unit, 'w') as f:
                    f.write('{}')
        except OSError as e:
            logging.warning("kiln_afc: can't prepare %s: %s", var, e)

    def _module_file(self, name):
        for d in self.dirs:
            if os.path.isfile(os.path.join(d, name + '.py')):
                return True
        return False

    def _load_object(self, config, section, *default):
        objects = self.printer.objects
        if section in objects:
            return objects[section]
        parts = section.split()
        if not parts or not self._module_file(parts[0]):
            return self._orig_load_object(config, section, *default)
        mod = importlib.import_module('extras.' + parts[0])
        init = getattr(mod, 'load_config_prefix' if len(parts) > 1
                       else 'load_config', None)
        if init is None:
            if default:
                return default[0]
            raise config.error("Unable to load module '%s'" % (section,))
        objects[section] = init(config.getsection(section))
        return objects[section]

    def get_status(self, eventtime):
        return {'afc_path': self.afc_dir}


def load_config(config):
    return KilnAFC(config)
