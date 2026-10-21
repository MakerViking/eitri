# Test-only klippy extra (never installed on a printer).
#
# QD_TEST_PWM VALUE=<0..1> pushes a duty cycle through the REAL Heater.set_pwm
# of the chamber heater, through the interlock's gate, and reports what would
# have reached the MCU output (a recorder replaces the MCU_pwm behind the gate
# for the duration of the call). Batch mode has no sensor readings, so this is
# the only way to see the gate act on a real Heater object.


class _Recorder:
    def __init__(self):
        self.calls = []

    def set_pwm(self, print_time, value):
        self.calls.append(value)

    def get_mcu(self):
        raise AssertionError("not used by Heater.set_pwm")


class TestPwm:
    def __init__(self, config):
        self.printer = config.get_printer()
        self.printer.lookup_object("gcode").register_command(
            "QD_TEST_PWM", self.cmd_QD_TEST_PWM)

    def cmd_QD_TEST_PWM(self, gcmd):
        value = gcmd.get_float("VALUE", minval=0., maxval=1.)
        heater = self.printer.lookup_object("heaters").lookup_heater("chamber")
        gate = heater.mcu_pwm
        real, recorder = gate._pwm, _Recorder()
        gate._pwm = recorder
        try:
            heater.verify_mainthread_time = 1e12     # as if the stats timer ran
            heater.next_pwm_time = 0.
            heater.last_pwm_value = 0.
            heater.set_pwm(1., value)
        finally:
            gate._pwm = real
        gcmd.respond_info("QD_TEST_PWM requested=%.3f reached_mcu=%s"
                          % (value, recorder.calls))


def load_config(config):
    return TestPwm(config)
