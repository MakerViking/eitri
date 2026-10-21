# Test-only klippy extra (never installed on a printer).
#
# Batch mode never triggers the probe. Loading [qd_test_probe] makes the probe
# report "triggered" in its status once the head is lifted, which is the state
# the "a pre-triggered probe is an error" rule has to handle.
from . import probe


def load_config(config):
    original = probe.PrinterProbe.get_status

    def get_status(self, eventtime):
        status = dict(original(self, eventtime))
        status["last_query"] = True
        return status

    probe.PrinterProbe.get_status = get_status
    return object()
