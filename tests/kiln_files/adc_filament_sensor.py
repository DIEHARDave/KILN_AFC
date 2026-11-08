# Filament runout sensor on an analog input (AD5X toolhead "filamentValue")
#
# The AD5X toolhead filament and cutter sensors are analog. Stock declares
# them as thermistors so their ADC gets sampled; this reads the same ADC
# (by its query_adc name) and feeds Klipper's standard runout handling
# (pause on runout, runout_gcode, insert_gcode). Filament counts as present
# when the value is at or below present_below or at or above present_above
# (stock head sensor: 0.30 and 0.72).
#
# The sensor is also published as "filament_switch_sensor <name>" (same
# object, same status), so screens that only know Klipper's switch sensors
# see it; HelixScreen's AD5X backend reads "filament_switch_sensor toolhead"
# as the toolhead filament sensor. SET_FILAMENT_SENSOR SENSOR=<name> works
# under both names.
#
# Copyright (C) 2026  ad5x-open contributors
#
# This file may be distributed under the terms of the GNU GPLv3 license.
from . import filament_switch_sensor

class AdcFilamentSensor:
    def __init__(self, config):
        self.printer = config.get_printer()
        self.reactor = self.printer.get_reactor()
        self.adc_name = config.get('adc')
        self.below = config.getfloat('present_below', minval=0., maxval=1.)
        self.above = config.getfloat('present_above', minval=0., maxval=1.)
        self.interval = config.getfloat('check_interval', .5, above=0.)
        self.value = None
        self.runout_helper = filament_switch_sensor.RunoutHelper(config)
        self.get_status = self.runout_helper.get_status
        self.printer.add_object("filament_switch_sensor %s"
                                % (config.get_name().split()[-1],), self)
        self.printer.register_event_handler("klippy:connect",
                                            self._handle_connect)
    def _handle_connect(self):
        qa = self.printer.lookup_object('query_adc', None)
        if qa is None or self.adc_name not in qa.adc:
            raise self.printer.config_error(
                "adc_filament_sensor: no ADC named '%s' (see QUERY_ADC)"
                % (self.adc_name,))
        self.adc = qa.adc[self.adc_name]
        self.reactor.register_timer(self._check, self.reactor.NOW)
    def _check(self, eventtime):
        self.value = self.adc.get_last_value()[1]
        present = self.value <= self.below or self.value >= self.above
        self.runout_helper.note_filament_present(eventtime, present)
        return eventtime + self.interval

def load_config_prefix(config):
    return AdcFilamentSensor(config)
