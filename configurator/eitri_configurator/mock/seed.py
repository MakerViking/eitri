"""Seed a scratch /data with what a freshly converted stock printer looks like,
so the Review step has a realistic "before" to diff against."""
from __future__ import annotations

from pathlib import Path

STOCK_PRINTER_CFG = """\
# Qidi stock-derived configuration (abbreviated for the mock)
[include plr.cfg]
[include gcode_macro.cfg]
[include KAMP_Settings.cfg]
[include box.cfg]

[mcu]
serial: /dev/ttyS2
baud: 500000

[mcu U_1]
serial: /dev/ttyS0
baud: 500000

[printer]
kinematics: corexy
max_velocity: 600
max_accel: 20000

[verify_heater extruder]
max_error: 120
check_gain_time: 20

#*# <---- SAVE_CONFIG ---->
#*# DO NOT EDIT THIS BLOCK OR BELOW. The contents are auto-generated.
#*#
#*# [qdprobe]
#*# z_offset = -0.012
#*#
#*# [input_shaper]
#*# shaper_type_x = mzv
#*# shaper_freq_x = 52.4
"""


def seed(data_dir: Path) -> None:
    cfg = data_dir / "printer_data" / "config"
    cfg.mkdir(parents=True, exist_ok=True)
    if not (cfg / "printer.cfg").exists():
        (cfg / "printer.cfg").write_text(STOCK_PRINTER_CFG)
    (data_dir / "eitri" / "configurator").mkdir(parents=True, exist_ok=True)
    # What camera/bin/eitri-go2rtc-auth writes on go2rtc's first start (a made-up password, for the screenshots).
    auth = cfg / "go2rtc-auth.env"
    if not auth.exists():
        auth.write_text("EITRI_RTSP_USER=eitri\nEITRI_RTSP_PASS=9f3c1a7e5b2d48c0a6e71d94f20b835c7a1e\n")
        auth.chmod(0o600)
