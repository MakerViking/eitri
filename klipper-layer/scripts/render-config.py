#!/usr/bin/env python3
"""Render the Eitri printer.cfg template.

The template (config/printer.cfg.tmpl) is a normal Klipper config with two
extra constructs, chosen so an unrendered template is still a comment-safe
file and so Klipper's own Jinja ({% %}, {}) is never touched:

    @@name@@          replaced by the value of variable `name`
    #@if name         lines up to #@else / #@endif are kept when `name` is
    #@if !name        true (or false for the !name form)
    #@else
    #@endif

Variables come from config/eitri-variables.toml (the table [vars]), then from
each --vars FILE in order, then from --set name=value. Unknown or unset
variables are errors; nothing is substituted silently.

printer.cfg is user-owned after the first render, so an existing output file
is never overwritten without --force.

Copyright 2026 The Eitri Authors. SPDX-License-Identifier: Apache-2.0
"""
import argparse
import datetime
import os
import re
import sys
import tempfile
import tomllib

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_DIR = os.path.join(os.path.dirname(HERE), "config")
NAME = r"[A-Za-z_][A-Za-z0-9_]*"
PLACEHOLDER = re.compile(r"@@(%s)@@" % NAME)
IF_RE = re.compile(r"^\s*#@if\s+(!?)(%s)\s*$" % NAME)
ELSE_RE = re.compile(r"^\s*#@else\s*$")
ENDIF_RE = re.compile(r"^\s*#@endif\s*$")


class RenderError(Exception):
    pass


def load_vars(path):
    with open(path, "rb") as f:
        data = tomllib.load(f)
    table = data.get("vars")
    if not isinstance(table, dict):
        raise RenderError("%s: missing [vars] table" % (path,))
    for key, value in table.items():
        if not re.fullmatch(NAME, key):
            raise RenderError("%s: bad variable name %r" % (path, key))
        if not isinstance(value, (bool, int, float, str)):
            raise RenderError("%s: variable %s must be a scalar" % (path, key))
    return table


def parse_set(item):
    name, sep, raw = item.partition("=")
    if not sep or not re.fullmatch(NAME, name):
        raise RenderError("--set expects name=value, got %r" % (item,))
    if raw.lower() in ("true", "false"):
        return name, raw.lower() == "true"
    for conv in (int, float):
        try:
            return name, conv(raw)
        except ValueError:
            pass
    return name, raw


def fmt(value):
    if isinstance(value, bool):
        return "True" if value else "False"
    return str(value)


def substitute(name, variables, lineno):
    if name not in variables:
        raise RenderError("line %d: unknown variable %s" % (lineno, name))
    return fmt(variables[name])


def render(template, variables):
    out = []
    stack = []          # (parent_active, branch_active, saw_else)
    active = True
    for lineno, line in enumerate(template.splitlines(), 1):
        m = IF_RE.match(line)
        if m:
            negate, name = m.groups()
            if name not in variables:
                raise RenderError("line %d: unknown variable %s" % (lineno, name))
            cond = bool(variables[name]) != bool(negate)
            stack.append((active, cond, False))
            active = active and cond
            continue
        if ELSE_RE.match(line):
            if not stack or stack[-1][2]:
                raise RenderError("line %d: stray #@else" % (lineno,))
            parent, cond, _ = stack[-1]
            stack[-1] = (parent, not cond, True)
            active = parent and not cond
            continue
        if ENDIF_RE.match(line):
            if not stack:
                raise RenderError("line %d: stray #@endif" % (lineno,))
            active = stack.pop()[0]
            continue
        if not active:
            continue

        out.append(PLACEHOLDER.sub(
            lambda m, n=lineno: substitute(m.group(1), variables, n), line))
    if stack:
        raise RenderError("unterminated #@if")
    text = "\n".join(out) + "\n"
    if "@@" in text:
        raise RenderError("unresolved '@@' left in output")
    return text


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--template",
                    default=os.path.join(CONFIG_DIR, "printer.cfg.tmpl"))
    ap.add_argument("--defaults",
                    default=os.path.join(CONFIG_DIR, "eitri-variables.toml"))
    ap.add_argument("--vars", action="append", default=[], metavar="FILE",
                    help="variables TOML applied over the defaults (repeatable)")
    ap.add_argument("--set", action="append", default=[], metavar="NAME=VALUE")
    ap.add_argument("--output", help="write here (default: stdout)")
    ap.add_argument("--force", action="store_true",
                    help="overwrite an existing --output file")
    args = ap.parse_args(argv)
    try:
        variables = load_vars(args.defaults)
        for path in args.vars:
            variables.update(load_vars(path))
        for item in args.set:
            name, value = parse_set(item)
            if name not in variables:
                raise RenderError("--set: unknown variable %s" % (name,))
            variables[name] = value
        with open(args.template, encoding="utf-8") as f:
            text = render(f.read(), variables)
    except (RenderError, OSError, tomllib.TOMLDecodeError) as e:
        print("render-config: %s" % (e,), file=sys.stderr)
        return 1
    header = ("# Generated by klipper-layer/scripts/render-config.py on %s.\n"
              "# This file is yours now: edit it freely. It is not "
              "re-rendered automatically.\n"
              % datetime.date.today().isoformat())
    text = header + text
    if not args.output:
        sys.stdout.write(text)
        return 0
    if os.path.exists(args.output) and not args.force:
        print("render-config: %s exists (use --force)" % (args.output,),
              file=sys.stderr)
        return 1
    outdir = os.path.dirname(os.path.abspath(args.output))
    os.makedirs(outdir, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=outdir, prefix=".printer.cfg.")
    try:
        os.fchmod(fd, 0o644)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, args.output)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise
    return 0


if __name__ == "__main__":
    sys.exit(main())
