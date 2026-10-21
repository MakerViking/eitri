# PARAMS policy for gcode_shell_command (deny extra arguments by default)
#
# RUN_SHELL_COMMAND CMD=<name> PARAMS=<...> appends PARAMS to the configured
# command line. Every G-code source (the API, macros, uploaded print files)
# can send it, so by default it would let any of them choose the arguments of
# a configured program. Eitri therefore refuses PARAMS unless the section
# opts in:
#
#   [gcode_shell_command beep]
#   command: /usr/bin/aplay
#   params_pattern: [A-Za-z0-9_./-]+\.wav   # PARAMS must match all of this
#
#   [gcode_shell_command tool]
#   command: /usr/local/bin/tool
#   allow_params: True                      # any PARAMS (the old behaviour)
#
# Without either option PARAMS must be empty. `params_pattern` is a regular
# expression the whole of PARAMS must match ('' allows none); with both
# options set the pattern still applies; `allow_params: False` together with
# `params_pattern` is a configuration error (the two contradict each other).
#
# This file is the single home of that policy:
#   * extras/gcode_shell_command.py (the module Eitri installs on mainline
#     Klipper) imports ParamsPolicy from here.
#   * Kalico ships its own, unrestricted gcode_shell_command module and the
#     overlay never shadows a file the clone tracks. Adding
#         [qd_shell_guard]
#     (flavors/kalico.cfg does) wraps Kalico's ShellCommand class so it reads
#     `allow_params` / `params_pattern` and enforces the same policy. The
#     section must come BEFORE every [gcode_shell_command ...] section; an
#     instance created earlier has no policy and refuses all commands rather
#     than running unguarded. On mainline Klipper the section is accepted and
#     does nothing (the installed module is already policy-aware).
#
# Copyright (C) 2026  The Eitri Authors
#
# SPDX-License-Identifier: GPL-3.0-or-later
# This file may be distributed under the terms of the GNU GPLv3 license.
import logging
import re
import shlex

ATTR = "_eitri_params_policy"
# Set on a gcode_shell_command module that enforces the policy itself
MODULE_MARKER = "EITRI_PARAMS_POLICY"


class ParamsPolicy:
    """What a [gcode_shell_command <name>] section allows in PARAMS."""

    def __init__(self, config, name):
        self.name = name
        self.allow = config.getboolean("allow_params", None)
        pattern = config.get("params_pattern", None)
        self.pattern = None
        if pattern is not None:
            if self.allow is False:
                raise config.error(
                    "gcode_shell_command %s: 'allow_params: False' and "
                    "'params_pattern' contradict each other" % (name,))
            try:
                self.pattern = re.compile(pattern)
            except re.error as e:
                raise config.error("gcode_shell_command %s: bad "
                                   "params_pattern: %s" % (name, e))

    def extra_args(self, params, error):
        """Return PARAMS split into arguments, or raise `error(message)`."""
        try:
            args = shlex.split(params)
        except ValueError as e:
            raise error("RUN_SHELL_COMMAND: bad PARAMS: %s" % (e,))
        if self.pattern is not None:
            if not self.pattern.fullmatch(params):
                raise error("RUN_SHELL_COMMAND: PARAMS for {%s} do not match "
                            "its params_pattern" % (self.name,))
        elif args and not self.allow:
            raise error("RUN_SHELL_COMMAND: {%s} does not accept PARAMS (add "
                        "'allow_params: True' to its [gcode_shell_command] "
                        "section, or restrict them with 'params_pattern')"
                        % (self.name,))
        return args


def _guard(cls):
    """Make a third-party ShellCommand class enforce ParamsPolicy."""
    if getattr(cls, "_eitri_guarded", False):
        return
    orig_init = cls.__init__
    orig_cmd = cls.cmd_RUN_SHELL_COMMAND

    def __init__(self, config):
        # Read first: the options are then known to the config checker, and
        # a bad value fails before the module registers its command.
        policy = ParamsPolicy(config, config.get_name().split()[-1])
        orig_init(self, config)
        setattr(self, ATTR, policy)

    def cmd_RUN_SHELL_COMMAND(self, gcmd):
        policy = getattr(self, ATTR, None)
        if policy is None:
            raise gcmd.error(
                "RUN_SHELL_COMMAND: refused, this gcode_shell_command section "
                "was loaded before [qd_shell_guard]; put [qd_shell_guard] "
                "first in the configuration")
        policy.extra_args(gcmd.get("PARAMS", ""), gcmd.error)
        return orig_cmd(self, gcmd)

    cls.__init__ = __init__
    cls.cmd_RUN_SHELL_COMMAND = cmd_RUN_SHELL_COMMAND
    cls._eitri_guarded = True


class ShellGuard:
    def __init__(self, config):
        from . import gcode_shell_command as host_module
        if getattr(host_module, MODULE_MARKER, False):
            logging.info("qd_shell_guard: gcode_shell_command already "
                         "enforces the PARAMS policy")
            return
        _guard(host_module.ShellCommand)
        logging.info("qd_shell_guard: PARAMS policy applied to the host's "
                     "gcode_shell_command")


def load_config(config):
    return ShellGuard(config)
