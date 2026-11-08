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

import importlib
import logging
import os
import sys

PLUGIN_DIR = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
DEFAULT_AFC_DIR = os.path.join(PLUGIN_DIR, 'AFC-Klipper-Add-On')
UNIT_DIR = os.path.join(PLUGIN_DIR, 'afc_ifs')
IFS_TYPE = 'IFS'


def _add_paths(afc_dir):
    import extras
    dirs = [UNIT_DIR, os.path.join(afc_dir, 'extras')]
    for d in dirs:
        if d not in extras.__path__:
            extras.__path__.append(d)
    return dirs


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
        self.afc_dir = config.get('afc_path', DEFAULT_AFC_DIR)
        if not os.path.isfile(os.path.join(self.afc_dir, 'extras', 'AFC.py')):
            raise config.error(
                "kiln_afc: no AFC checkout at %s. Run install.sh in the "
                "plugin folder (it clones AFC-Klipper-Add-On)." % self.afc_dir)
        self.dirs = _add_paths(self.afc_dir)
        _teach_unit_type()
        self._prepare_varfile(config)
        self._orig_load_object = printer.load_object
        printer.load_object = self._load_object
        logging.info("kiln_afc: AFC modules from %s", ", ".join(self.dirs))

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
