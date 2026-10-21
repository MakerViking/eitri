# Power-loss recovery (PLR) scaffold
#
# Records print progress while a virtual_sdcard print runs and, after an
# unexpected power cut, rebuilds a resume G-code file from the interrupted
# file. This replaces the hard-coded /home/mks/scripts/plr/ hook that QIDI's
# fork compiled into virtual_sdcard.py, and needs no helper script.
#
# How it works
#   * While a print is running a 1 s timer samples (file, byte position,
#     heater targets, fan, Z offset, bed mesh profile) and writes it to
#     state_file at most every save_interval seconds, atomically and from a
#     worker thread (fsync on eMMC must not stall the reactor). A completed
#     or cancelled print removes the record. A power cut, an MCU shutdown or a
#     Klipper error leaves it behind.
#   * After the next start the record is exposed as
#     printer.qd_plr.recovery_available, which macros/UIs can offer as a
#     prompt.
#   * QD_PLR_RESUME CONFIRM=1 scans the interrupted file up to the recorded
#     byte to recover the toolhead state (X/Y/Z/E/F, positioning modes,
#     speed/accel settings, object-exclusion state), writes
#     <path>/.plr/resume-{a,b}.gcode = [re-heat, restore position, restore
#     state] + [rest of the original file], and starts printing it.
#
# SAFETY
#   After a power cut the machine has lost its position. The generated file
#   trusts the recorded Z (SET_KINEMATIC_POSITION), lifts away from the part,
#   homes X/Y and returns. That is only valid if the bed did not move while
#   unpowered, so QD_PLR_RESUME refuses to act without CONFIRM=1. It never
#   probes, never homes onto the print and never silences a heater check.
#
# Needs [virtual_sdcard], [force_move] with enable_force_move: True (for
# SET_KINEMATIC_POSITION) and [respond]-free G-code only. The power-cut flow
# itself still needs validation on a real printer (see klipper-layer README).
#
# Copyright (C) 2026  The Eitri Authors
#
# SPDX-License-Identifier: GPL-3.0-or-later
# This file may be distributed under the terms of the GNU GPLv3 license.
import json
import logging
import math
import os
import re
import shutil
import threading
import time

STATE_VERSION = 1
COPY_CHUNK = 1 << 20
FREE_SPACE_MARGIN = 8 << 20
RESUME_FILES = ("resume-a.gcode", "resume-b.gcode")
RESUME_DIR = ".plr"

_WORD_RE = re.compile(r"([A-Za-z])\s*([-+]?(?:\d+\.?\d*|\.\d+))")
_GM_RE = re.compile(r"^([GM])0*(\d+)(?![\d.])", re.I)
# Commands whose last value must survive the cut; keyed by (command, params)
_STICKY_G = {"M204", "M220", "M221", "M900"}
_STICKY_EXT = {"SET_VELOCITY_LIMIT", "SET_PRESSURE_ADVANCE",
               "SET_INPUT_SHAPER", "SET_RETRACTION"}


######################################################################
# G-code scanning (pure functions; unit tested without Klipper)
######################################################################

class ScanState:
    """Toolhead/G-code state reconstructed by replaying a file prefix."""

    def __init__(self):
        self.pos = {"X": None, "Y": None, "Z": None, "E": None}
        self.feed = None
        self.absolute_xyz = True
        self.absolute_e = True
        self.sticky = {}
        self.exclude_defs = []
        self.exclude_excluded = []
        self.exclude_current = None
        self.stats_info = {}
        self.lines = 0

    @property
    def e_is_absolute(self):
        # Klipper treats E as relative if either G91 or M83 is active
        return self.absolute_xyz and self.absolute_e


def _words(text):
    return {m.group(1).upper(): float(m.group(2))
            for m in _WORD_RE.finditer(text)}


def _ext_params(rest):
    params = {}
    for tok in rest.split():
        if "=" in tok:
            k, _, v = tok.partition("=")
            params[k.upper()] = v
    return params


def _apply_move(st, w):
    for axis in "XYZ":
        if axis in w:
            if st.absolute_xyz:
                st.pos[axis] = w[axis]
            elif st.pos[axis] is not None:
                st.pos[axis] += w[axis]
    if "E" in w:
        if st.e_is_absolute:
            st.pos["E"] = w["E"]
        elif st.pos["E"] is not None:
            st.pos["E"] += w["E"]
    if "F" in w:
        st.feed = w["F"]


def scan_line(st, raw):
    """Fold one G-code line into st."""
    line = raw.split(";", 1)[0].strip()
    if not line:
        return
    st.lines += 1
    m = _GM_RE.match(line)
    if m:
        code = m.group(1).upper() + str(int(m.group(2)))
        rest = line[m.end():]
        if code in ("G0", "G1", "G2", "G3"):
            _apply_move(st, _words(rest))
        elif code == "G92":
            w = _words(rest)
            if not any(a in w for a in "XYZE"):
                w = {"X": 0., "Y": 0., "Z": 0., "E": 0.}
            for axis in "XYZE":
                if axis in w:
                    st.pos[axis] = w[axis]
        elif code == "G90":
            st.absolute_xyz = True
        elif code == "G91":
            st.absolute_xyz = False
        elif code == "M82":
            st.absolute_e = True
        elif code == "M83":
            st.absolute_e = False
        elif code in _STICKY_G:
            names = tuple(sorted(_words(rest)))
            st.sticky[(code, names)] = "%s%s" % (code, rest)
        return
    cmd, _, rest = line.partition(" ")
    cmd = cmd.upper()
    if cmd in _STICKY_EXT:
        params = _ext_params(rest)
        key = (cmd, tuple(sorted(params)))
        st.sticky[key] = "%s %s" % (cmd, rest.strip())
    elif cmd == "SET_PRINT_STATS_INFO":
        st.stats_info.update(_ext_params(rest))
    elif cmd == "EXCLUDE_OBJECT_DEFINE":
        st.exclude_defs.append(line)
    elif cmd == "EXCLUDE_OBJECT_START":
        st.exclude_current = line
    elif cmd == "EXCLUDE_OBJECT_END":
        st.exclude_current = None
    elif cmd == "EXCLUDE_OBJECT":
        if "NAME=" in line.upper():
            st.exclude_excluded.append(line)


def scan_gcode(path, end_pos, yield_cb=None):
    """Replay path[0:end_pos] (a line boundary) and return the ScanState."""
    st = ScanState()
    done = 0
    with open(path, "rb") as f:
        while done < end_pos:
            chunk = f.read(min(COPY_CHUNK, end_pos - done))
            if not chunk:
                break
            done += len(chunk)
            for raw in chunk.decode("utf-8", "replace").splitlines():
                scan_line(st, raw)
            if yield_cb is not None:
                yield_cb()
    return st


def _fmt(value):
    return ("%.5f" % value).rstrip("0").rstrip(".") or "0"


def build_resume_header(rec, st, opts):
    """G-code that re-establishes heaters, position and state.

    rec  -- the state record written during the print
    st   -- ScanState of the file up to rec['position']
    opts -- dict: z_lift, home_gcode, purge_macro, use_interlock, fan_name
    Returns (lines, warnings).
    """
    warnings = []
    x, y, z = st.pos["X"], st.pos["Y"], st.pos["Z"]
    if None in (x, y, z):
        raise ValueError("the file had not reached its first absolute X/Y/Z "
                         "move at the recorded position (the print was still "
                         "in its start-up macro); restart the print instead")
    zoff = float(rec.get("z_offset", 0.))
    real_z = z + zoff
    lift = float(opts["z_lift"])
    out = ["; ---- Eitri qd_plr resume header (generated) ----",
           "; source: %s @ byte %d, saved %s" % (
               rec["file"], rec["position"],
               time.strftime("%Y-%m-%d %H:%M:%S",
                             time.localtime(rec.get("saved_at", 0)))),
           "; resume point: X%s Y%s Z%s (gcode) = Z%s (machine)" % (
               _fmt(x), _fmt(y), _fmt(z), _fmt(real_z))]
    bed, ext = rec.get("bed_target", 0.), rec.get("extruder_target", 0.)
    chamber = rec.get("chamber_target", 0.)
    saved_tz = rec.get("toolhead_z")
    if saved_tz is not None and abs(saved_tz - real_z) > 3.0:
        warnings.append(
            "recorded toolhead Z %.2f differs from the replayed Z %.2f by "
            "more than 3 mm; check the bed height before continuing"
            % (saved_tz, real_z))
    # Heat the bed first (it takes longest); hold the nozzle cool until the
    # head is clear of the part so it cannot ooze or burn the print.
    out.append("; -- heaters")
    if bed > 0:
        out.append("M140 S%s" % _fmt(bed))
    if chamber > 0:
        if opts.get("use_interlock"):
            out.append("QD_SET_CHAMBER TARGET=%s" % _fmt(chamber))
        else:
            out.append("SET_HEATER_TEMPERATURE HEATER=chamber TARGET=%s"
                       % _fmt(chamber))
    if bed > 0:
        out.append("M190 S%s" % _fmt(bed))
    # Position: trust the recorded Z, lift away, home X/Y, come back.
    out += ["; -- position (Z is trusted, see qd_plr SAFETY notes)",
            "SET_GCODE_OFFSET Z=0 MOVE=0",
            "SET_KINEMATIC_POSITION Z=%s SET_HOMED=z" % _fmt(real_z),
            "G91",
            "G1 Z%s F600" % _fmt(lift),
            "G90",
            opts["home_gcode"]]
    out.append("; -- restore settings")
    out.append("SET_GCODE_OFFSET Z=%s MOVE=0" % _fmt(zoff))
    profile = rec.get("bed_mesh_profile")
    if profile:
        out.append("BED_MESH_PROFILE LOAD=%s" % profile)
    if ext > 0:
        out.append("M109 S%s" % _fmt(ext))
    if ext > 0 and opts.get("purge_macro"):
        out.append(opts["purge_macro"])
    out += [line for _, line in sorted(st.sticky.items(), key=str)]
    fan = rec.get("fan")
    if fan is not None:
        name = opts.get("fan_name", "fan")
        if name.startswith("fan_generic "):
            out.append("SET_FAN_SPEED FAN=%s SPEED=%s"
                       % (name.split(None, 1)[1], _fmt(fan)))
        else:
            out.append("M106 S%d" % round(255 * fan))
    if st.stats_info:
        out.append("SET_PRINT_STATS_INFO " + " ".join(
            "%s=%s" % kv for kv in sorted(st.stats_info.items())))
    out += st.exclude_defs + st.exclude_excluded
    if st.exclude_current:
        out.append(st.exclude_current)
    out.append("; -- return to the print")
    out += ["G90",
            "G1 X%s Y%s F9000" % (_fmt(x), _fmt(y)),
            "G1 Z%s F600" % _fmt(z)]
    out.append("M82" if st.absolute_e else "M83")
    if st.e_is_absolute and st.pos["E"] is not None:
        out.append("G92 E%s" % _fmt(st.pos["E"]))
    else:
        out.append("G92 E0")
    out.append("G90" if st.absolute_xyz else "G91")
    if st.feed:
        out.append("G1 F%s" % _fmt(st.feed))
    out.append("; ---- end of resume header ----")
    return out, warnings


######################################################################
# Atomic state file handling
######################################################################

def atomic_write(path, data):
    directory = os.path.dirname(path) or "."
    tmp = "%s.tmp%d" % (path, os.getpid())
    with open(tmp, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


class StateWriter:
    """Latest-wins background writer so fsync never blocks the reactor."""

    def __init__(self, path):
        self.path = path
        self.cond = threading.Condition()
        self.pending = None
        self.busy = False
        self.thread = None

    def submit(self, data):
        with self.cond:
            self.pending = data
            if self.thread is None:
                self.thread = threading.Thread(target=self._run, daemon=True,
                                               name="qd_plr_writer")
                self.thread.start()
            self.cond.notify_all()

    def _run(self):
        while True:
            with self.cond:
                while self.pending is None:
                    self.cond.wait()
                data, self.pending = self.pending, None
                self.busy = True
            try:
                atomic_write(self.path, data)
            except OSError:
                logging.exception("qd_plr: unable to write %s", self.path)
            with self.cond:
                self.busy = False
                self.cond.notify_all()

    def drain(self, timeout=5.):
        """Wait until queued data has been written (shutdown and tests)."""
        deadline = time.monotonic() + timeout
        with self.cond:
            while self.pending is not None or self.busy:
                left = deadline - time.monotonic()
                if left <= 0:
                    return False
                self.cond.wait(left)
        return True

    def discard(self):
        """Drop queued data and remove the file."""
        self.drain()
        with self.cond:
            self.pending = None
            try:
                os.unlink(self.path)
            except FileNotFoundError:
                pass


def load_record(path):
    """Return the validated record at path, None if absent/corrupt."""
    try:
        with open(path, "rb") as f:
            rec = json.loads(f.read().decode("utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        logging.exception("qd_plr: unreadable state file %s", path)
        return None
    try:
        if (rec["version"] != STATE_VERSION or not isinstance(rec["file"], str)
                or not isinstance(rec["position"], int) or rec["position"] < 0
                or os.path.isabs(rec["file"])
                or ".." in rec["file"].split("/")):
            raise ValueError
        for key in ("bed_target", "extruder_target", "chamber_target"):
            if not math.isfinite(float(rec.get(key, 0.))):
                raise ValueError
    except (KeyError, TypeError, ValueError):
        logging.error("qd_plr: ignoring malformed state file %s", path)
        return None
    return rec


######################################################################
# Klipper object
######################################################################

class QdPlr:
    def __init__(self, config):
        self.printer = config.get_printer()
        self.gcode = self.printer.lookup_object("gcode")
        self.sd = self.printer.load_object(config, "virtual_sdcard")
        self.print_stats = self.printer.load_object(config, "print_stats")
        self.printer.load_object(config, "heaters")
        default_state = os.path.join(self.sd.sdcard_dirname, RESUME_DIR,
                                     "state.json")
        self.state_file = os.path.expanduser(
            config.get("state_file", default_state))
        self.plr_dir = os.path.join(self.sd.sdcard_dirname, RESUME_DIR)
        self.save_interval = config.getfloat("save_interval", 10., minval=1.)
        self.z_lift = config.getfloat("z_lift", 10., minval=1., maxval=50.)
        self.home_gcode = config.get("home_gcode", "G28 X Y")
        self.purge_macro = config.get("purge_macro", "").strip()
        self.fan_name = config.get("fan", "fan")
        self.extruder_name = config.get("extruder", "extruder")
        self.bed_name = config.get("bed_heater", "heater_bed")
        self.chamber_name = config.get("chamber_heater", "chamber")
        os.makedirs(os.path.dirname(self.state_file), exist_ok=True)
        os.makedirs(self.plr_dir, exist_ok=True)
        self.writer = StateWriter(self.state_file)
        self.record = None
        self.tracking = False
        self.last_saved_pos = None
        self.last_save_time = None
        self.printer.register_event_handler("klippy:ready", self._handle_ready)
        self.printer.register_event_handler("klippy:disconnect",
                                           self._handle_disconnect)
        for name, func, desc in (
                ("QD_PLR_STATUS", self.cmd_QD_PLR_STATUS,
                 "Report the recorded interrupted print"),
                ("QD_PLR_CLEAR", self.cmd_QD_PLR_CLEAR,
                 "Forget the recorded interrupted print"),
                ("QD_PLR_RESUME", self.cmd_QD_PLR_RESUME,
                 "Resume the interrupted print: QD_PLR_RESUME CONFIRM=1 "
                 "[Z_LIFT=<mm>]")):
            self.gcode.register_command(name, func, desc=desc)

    # Lifecycle -------------------------------------------------------

    def _handle_ready(self):
        self.record = load_record(self.state_file)
        if self.record is not None:
            logging.info("qd_plr: interrupted print found: %s @ %d",
                         self.record["file"], self.record["position"])
        reactor = self.printer.get_reactor()
        reactor.register_timer(self._timer_event, reactor.monotonic() + 1.)

    def _handle_disconnect(self):
        self.writer.drain()

    def _timer_event(self, eventtime):
        try:
            self._tick(eventtime)
        except Exception:
            logging.exception("qd_plr: tracking error")
        return eventtime + 1.

    # Tracking --------------------------------------------------------

    def _target(self, heater_name, eventtime):
        try:
            heater = self.printer.lookup_object("heaters").lookup_heater(
                heater_name.split()[-1])
        except self.printer.config_error:
            return 0.
        return heater.get_temp(eventtime)[1]

    def _snapshot(self, eventtime, pos, path, state):
        gm = self.printer.lookup_object("gcode_move").get_status(eventtime)
        stats = self.print_stats.get_status(eventtime)
        bed_mesh = self.printer.lookup_object("bed_mesh", None)
        fan = self.printer.lookup_object(self.fan_name, None)
        rel = os.path.relpath(path, self.sd.sdcard_dirname)
        return {
            "version": STATE_VERSION,
            "saved_at": time.time(),
            "file": rel,
            "file_size": self.sd.file_size,
            "position": int(pos),
            "state": state,
            "extruder_target": self._target(self.extruder_name, eventtime),
            "bed_target": self._target(self.bed_name, eventtime),
            "chamber_target": self._target(self.chamber_name, eventtime),
            "fan": (fan.get_status(eventtime)["speed"] if fan else None),
            "z_offset": gm["homing_origin"][2],
            "toolhead_z": gm["position"][2],
            "speed_factor": gm["speed_factor"],
            "extrude_factor": gm["extrude_factor"],
            "bed_mesh_profile": (bed_mesh.get_status(eventtime).get(
                "profile_name", "") if bed_mesh else ""),
            "current_layer": stats["info"]["current_layer"],
            "total_layer": stats["info"]["total_layer"],
        }

    def _tick(self, eventtime):
        state = self.print_stats.get_status(eventtime)["state"]
        if state == "printing":
            self.tracking = True
            path = self.sd.file_path()
            pos = self.sd.file_position
            if path is None or pos == self.last_saved_pos:
                return
            if (self.last_save_time is not None
                    and eventtime - self.last_save_time < self.save_interval):
                return
            rec = self._snapshot(eventtime, pos, path, state)
            self.writer.submit(json.dumps(rec).encode("utf-8"))
            self.record = rec
            self.last_saved_pos = pos
            self.last_save_time = eventtime
        elif state == "error":
            # Keep the record: a failed print can be resumed
            self.tracking = False
        elif state in ("complete", "cancelled", "standby") and self.tracking:
            # The cancel macro may reset the file before the next tick, so a
            # print that returns to standby without an error counts as ended
            self.tracking = False
            self._forget()
        # paused: heaters are parked, so the last in-print sample is the
        # better one to resume from; nothing is written

    def _forget(self):
        self.writer.discard()
        self.record = None
        self.last_saved_pos = self.last_save_time = None
        for name in RESUME_FILES:
            try:
                os.unlink(os.path.join(self.plr_dir, name))
            except FileNotFoundError:
                pass

    # G-Code ----------------------------------------------------------

    def _describe(self):
        rec = self.record
        if rec is None:
            return "No interrupted print is recorded."
        pct = (100. * rec["position"] / rec["file_size"]
               if rec.get("file_size") else 0.)
        return ("Interrupted print: %s at byte %d (%.1f%%), layer %s, "
                "targets hotend %.0f bed %.0f chamber %.0f"
                % (rec["file"], rec["position"], pct,
                   rec.get("current_layer"), rec.get("extruder_target", 0),
                   rec.get("bed_target", 0), rec.get("chamber_target", 0)))

    def cmd_QD_PLR_STATUS(self, gcmd):
        gcmd.respond_info(self._describe())

    def cmd_QD_PLR_CLEAR(self, gcmd):
        self._forget()
        gcmd.respond_info("Interrupted-print record cleared.")

    def cmd_QD_PLR_RESUME(self, gcmd):
        rec = self.record
        if rec is None:
            raise gcmd.error("No interrupted print is recorded")
        state = self.print_stats.get_status(
            self.printer.get_reactor().monotonic())["state"]
        if state in ("printing", "paused"):
            raise gcmd.error("A print is active; finish or cancel it first")
        lift = gcmd.get_float("Z_LIFT", self.z_lift, minval=1., maxval=50.)
        src = os.path.join(self.sd.sdcard_dirname, rec["file"])
        if (not os.path.isfile(src)
                or os.path.getsize(src) < rec["position"]):
            raise gcmd.error("Recorded file %s is missing or changed"
                             % (rec["file"],))
        if not gcmd.get_int("CONFIRM", 0, minval=0, maxval=1):
            gcmd.respond_info(
                self._describe() + "\nThe printer lost its position at power "
                "loss. Resuming assumes the bed has NOT moved: the recorded Z "
                "is trusted, the head lifts %.0f mm, homes X/Y and returns to "
                "the part. Make sure the nozzle is not stuck in the print, "
                "then run QD_PLR_RESUME CONFIRM=1." % (lift,))
            return
        self._resume(gcmd, rec, src, lift)

    def _resume(self, gcmd, rec, src, lift):
        reactor = self.printer.get_reactor()
        gcmd.respond_info("Replaying %s to the resume point..." % (rec["file"],))
        try:
            st = scan_gcode(src, rec["position"],
                            lambda: reactor.pause(reactor.NOW))
            use_interlock = (self.printer.lookup_object(
                "qd_power_interlock", None) is not None)
            header, warnings = build_resume_header(rec, st, {
                "z_lift": lift, "home_gcode": self.home_gcode,
                "purge_macro": self.purge_macro,
                "use_interlock": use_interlock, "fan_name": self.fan_name})
        except (OSError, ValueError) as e:
            raise gcmd.error("Cannot build the resume file: %s" % (e,))
        for w in warnings:
            gcmd.respond_info("WARNING: " + w)
        axis_max = self.printer.lookup_object("toolhead").get_status(
            reactor.monotonic())["axis_maximum"][2]
        real_z = st.pos["Z"] + float(rec.get("z_offset", 0.))
        if real_z + lift > axis_max:
            raise gcmd.error("Z lift of %.0f mm from Z%.2f exceeds the %.0f mm "
                             "axis limit; use Z_LIFT=" % (lift, real_z,
                                                          axis_max))
        current = os.path.basename(rec["file"]) if os.path.dirname(
            rec["file"]) == RESUME_DIR else None
        name = RESUME_FILES[1] if current == RESUME_FILES[0] else RESUME_FILES[0]
        dst = os.path.join(self.plr_dir, name)
        self._write_resume_file(gcmd, header, src, rec["position"], dst)
        self.last_saved_pos = self.last_save_time = None
        rel = os.path.join(RESUME_DIR, name)
        self.gcode.run_script_from_command("SDCARD_PRINT_FILE FILENAME=%s"
                                           % (rel,))
        # Even a print that finishes between two ticks must retire the record
        self.tracking = True

    def _write_resume_file(self, gcmd, header, src, start, dst):
        reactor = self.printer.get_reactor()
        need = os.path.getsize(src) - start + FREE_SPACE_MARGIN
        if shutil.disk_usage(self.plr_dir).free < need:
            raise gcmd.error("Not enough free space for the resume file")
        tmp = dst + ".tmp"
        try:
            with open(src, "rb") as fin, open(tmp, "wb") as fout:
                fout.write(("\n".join(header) + "\n").encode("utf-8"))
                fin.seek(start)
                while True:
                    chunk = fin.read(COPY_CHUNK)
                    if not chunk:
                        break
                    fout.write(chunk)
                    reactor.pause(reactor.NOW)
                fout.flush()
                os.fsync(fout.fileno())
            os.replace(tmp, dst)
        except OSError as e:
            raise gcmd.error("Unable to write the resume file: %s" % (e,))

    def get_status(self, eventtime):
        rec = self.record
        return {
            "recovery_available": rec is not None and not self.tracking,
            "tracking": self.tracking,
            "file": rec["file"] if rec else None,
            "position": rec["position"] if rec else None,
            "saved_at": rec["saved_at"] if rec else None,
            "extruder_target": rec["extruder_target"] if rec else None,
            "bed_target": rec["bed_target"] if rec else None,
        }


def load_config(config):
    return QdPlr(config)
