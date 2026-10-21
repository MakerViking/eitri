# Run a configured host command from G-code (RUN_SHELL_COMMAND)
#
# Clean implementation of the long-standing "gcode_shell_command" extension.
# It keeps the configuration and G-code interface of the community module
# (originally by Eric Callahan, distributed with KIAUH) so existing configs
# and macros keep working, but the code is written from that interface
# description and fixes its known defects:
#   * the child's output pipe is unregistered on EOF (the original left a
#     permanently-readable fd registered, which spins the reactor),
#   * a read error can no longer leave the output buffer unbound,
#   * a timed-out child is terminated, then killed, then reaped (the original
#     only sent SIGTERM and left a zombie behind),
#   * the child gets its own session so a timeout also takes down anything the
#     command spawned, and never inherits Klipper's stdin.
#
# Commands are executed directly (argv), never through a shell. PARAMS is
# split with shlex and appended as additional arguments, verbatim.
#
# SECURITY: every G-code source (the API, macros, uploaded print files) can
# supply PARAMS, so it would choose extra arguments for the configured
# program. Eitri therefore DENIES PARAMS by default: RUN_SHELL_COMMAND with a
# non-empty PARAMS is refused unless the section opts in, either with
# `params_pattern` (a regular expression PARAMS must match completely; '' allows
# none) or with `allow_params: True` (anything, as in the community module).
# The policy lives in qd_shell_guard.py, which also applies it to Kalico's own
# module. Example:
#
#   [gcode_shell_command beep]
#   command: /usr/bin/aplay
#   params_pattern: [A-Za-z0-9_./-]+\.wav
#
# Kalico already ships a module with this name; overlay-install.sh therefore
# does not link this file into a Kalico tree (it never shadows a tracked
# file); [qd_shell_guard] makes Kalico's copy honour the same PARAMS options.
# Only the options shared by both implementations are used in the shipped
# configs: command, timeout, verbose (plus allow_params / params_pattern,
# which both accept).
#
# Copyright (C) 2026  The Eitri Authors
#
# SPDX-License-Identifier: GPL-3.0-or-later
# This file may be distributed under the terms of the GNU GPLv3 license.
import logging
import os
import shlex
import signal
import subprocess

from . import qd_shell_guard

# Set so [qd_shell_guard] knows this module enforces the PARAMS policy itself
EITRI_PARAMS_POLICY = True

# Grace period between SIGTERM and SIGKILL for a command that hit its timeout
KILL_GRACE_TIME = 1.0
POLL_INTERVAL = 0.05
READ_SIZE = 4096


class ShellCommand:
    def __init__(self, config):
        self.printer = config.get_printer()
        self.name = config.get_name().split()[-1]
        self.gcode = self.printer.lookup_object("gcode")
        command = os.path.expandvars(os.path.expanduser(config.get("command")))
        try:
            self.command = shlex.split(command)
        except ValueError as e:
            raise config.error("gcode_shell_command %s: bad command: %s"
                               % (self.name, e))
        if not self.command:
            raise config.error("gcode_shell_command %s: command is empty"
                               % (self.name,))
        self.timeout = config.getfloat("timeout", 2.0, above=0.0)
        self.policy = qd_shell_guard.ParamsPolicy(config, self.name)
        self.verbose = config.getboolean("verbose", True)
        self.last_returncode = None
        self._running = False
        self.gcode.register_mux_command(
            "RUN_SHELL_COMMAND", "CMD", self.name, self.cmd_RUN_SHELL_COMMAND,
            desc=self.cmd_RUN_SHELL_COMMAND_help)

    def get_status(self, eventtime):
        return {"running": self._running,
                "last_returncode": self.last_returncode}

    # Output handling -------------------------------------------------

    def _make_reader(self, fd, sink):
        """Return a reactor fd callback that forwards complete lines."""
        state = {"partial": b"", "eof": False}

        def on_readable(eventtime):
            if state["eof"]:
                return
            try:
                data = os.read(fd, READ_SIZE)
            except BlockingIOError:
                return
            except OSError:
                data = b""
            if not data:
                state["eof"] = True
                return
            buf = state["partial"] + data
            head, sep, tail = buf.rpartition(b"\n")
            if sep:
                sink(head.decode("utf-8", "replace"))
            state["partial"] = tail

        def flush():
            if state["partial"]:
                sink(state["partial"].decode("utf-8", "replace"))
                state["partial"] = b""

        return on_readable, flush, state

    def _drain(self, on_readable, state):
        """Read what is left in the pipe after the child exited."""
        while not state["eof"]:
            before = state["partial"]
            on_readable(0.)
            if not state["eof"] and before == state["partial"]:
                break

    # Process control -------------------------------------------------

    def _stop(self, proc, reactor):
        """Terminate, then kill, the whole process group; reap the child."""
        for sig in (signal.SIGTERM, signal.SIGKILL):
            try:
                os.killpg(proc.pid, sig)
            except ProcessLookupError:
                break
            except PermissionError:
                proc.send_signal(sig)
            deadline = reactor.monotonic() + KILL_GRACE_TIME
            while proc.poll() is None and reactor.monotonic() < deadline:
                reactor.pause(reactor.monotonic() + POLL_INTERVAL)
            if proc.poll() is not None:
                break
        try:
            proc.wait(timeout=KILL_GRACE_TIME)
        except subprocess.TimeoutExpired:
            logging.error("gcode_shell_command %s: child %d did not exit",
                          self.name, proc.pid)

    cmd_RUN_SHELL_COMMAND_help = "Run a configured host command"

    def cmd_RUN_SHELL_COMMAND(self, gcmd):
        extra_args = self.policy.extra_args(gcmd.get("PARAMS", ""),
                                            gcmd.error)
        reactor = self.printer.get_reactor()
        try:
            proc = subprocess.Popen(
                self.command + extra_args,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE if self.verbose else subprocess.DEVNULL,
                stderr=subprocess.STDOUT,
                close_fds=True,
                start_new_session=True)
        except OSError as e:
            logging.exception("gcode_shell_command %s failed to start",
                              self.name)
            raise gcmd.error("Error running command {%s}: %s"
                             % (self.name, e))
        handle = on_readable = flush = state = None
        if self.verbose:
            fd = proc.stdout.fileno()
            os.set_blocking(fd, False)
            on_readable, flush, state = self._make_reader(fd,
                                                          gcmd.respond_info)
            gcmd.respond_info("Running Command {%s}...:" % (self.name,))
            handle = reactor.register_fd(fd, on_readable)
        self._running = True
        timed_out = False
        try:
            deadline = reactor.monotonic() + self.timeout
            eventtime = reactor.monotonic()
            while proc.poll() is None:
                if eventtime >= deadline:
                    timed_out = True
                    break
                eventtime = reactor.pause(eventtime + POLL_INTERVAL)
            if timed_out:
                self._stop(proc, reactor)
            if self.verbose:
                self._drain(on_readable, state)
                flush()
        finally:
            self._running = False
            if handle is not None:
                reactor.unregister_fd(handle)
            if proc.stdout is not None:
                proc.stdout.close()
            if proc.poll() is None:
                self._stop(proc, reactor)
        self.last_returncode = proc.returncode
        if self.verbose:
            if timed_out:
                msg = "timed out"
            elif proc.returncode:
                msg = "finished with exit code %d" % (proc.returncode,)
            else:
                msg = "finished"
            gcmd.respond_info("Command {%s} %s" % (self.name, msg))


def load_config_prefix(config):
    return ShellCommand(config)
