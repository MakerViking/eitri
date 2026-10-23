# SPDX-License-Identifier: Apache-2.0
"""eitri-hosts: the service user's list of extra host names becomes a validated nginx map, or nothing."""
from __future__ import annotations

import os
import re
import subprocess
import sys

import pytest
from conftest import OVERLAY

HELPER = OVERLAY / "usr/libexec/eitri/eitri-hosts"


@pytest.fixture
def box(tmp_path):
    root = tmp_path / "root"
    (root / "etc/nginx").mkdir(parents=True)
    (root / "data/printer_data/config").mkdir(parents=True)
    log = tmp_path / "calls.log"
    nginx = tmp_path / "nginx"
    nginx.write_text(f'#!/bin/sh\necho "nginx $@" >> "{log}"\nexit "${{NGINX_EXIT:-0}}"\n')
    systemctl = tmp_path / "systemctl"
    systemctl.write_text(f'#!/bin/sh\necho "systemctl $@" >> "{log}"\nexit 0\n')
    nginx.chmod(0o755)
    systemctl.chmod(0o755)
    env = {**{k: v for k, v in os.environ.items() if not k.startswith("EITRI_")},
           "EITRI_HOSTS_ROOT": str(root), "EITRI_HOSTS_NGINX": str(nginx), "EITRI_HOSTS_SYSTEMCTL": str(systemctl)}
    return {"root": root, "env": env, "log": log, "src": root / "data/printer_data/config/eitri-hosts.txt",
            "map": root / "etc/nginx/eitri-hosts.map"}


def hosts(box, extra_env=None):
    return subprocess.run([sys.executable, "-I", HELPER], env={**box["env"], **(extra_env or {})},
                          capture_output=True, text=True)


def entries(box):
    return [ln for ln in box["map"].read_text().splitlines() if ln and not ln.startswith("#")]


def test_valid_names_become_quoted_map_keys_and_nginx_reloads(box):
    box["src"].write_text("# my router\nPrinter.Home.Arpa\n\nvoron.lan, x-plus-4.example.org   # trailing note\n")
    result = hosts(box)
    assert result.returncode == 0, result.stderr
    assert entries(box) == ['"printer.home.arpa" 1;', '"voron.lan" 1;', '"x-plus-4.example.org" 1;']
    calls = box["log"].read_text()
    assert "nginx -t -q" in calls and "systemctl reload --no-block nginx.service" in calls


@pytest.mark.parametrize("bad", [
    '"evil" 1; } server { listen 81;', "a;b", "under_score.lan", "-dash.lan", "dash-.lan", "a..b", "x" * 64 + ".lan",
    "spaces\tare.fine evil{", "ünïcode.lan", "*.wild.card",
])
def test_anything_but_a_dns_name_is_skipped_never_copied(box, bad):
    box["src"].write_text(f"good.lan\n{bad}\n")
    result = hosts(box)
    assert result.returncode == 0
    # The injection-proof property: every line nginx reads is one quoted DNS name, whatever the input held
    # (pieces of the input that happen to be valid names, like "server", are harmless map keys).
    for line in entries(box):
        assert re.fullmatch(r'"[a-z0-9]([a-z0-9.-]*[a-z0-9])?" 1;', line), (bad, line)
    assert '"good.lan" 1;' in entries(box)
    assert "skipped" in result.stderr


def test_a_missing_file_means_no_extra_names(box):
    assert hosts(box).returncode == 0
    assert entries(box) == []


def test_nothing_changes_when_the_names_are_the_same(box):
    box["src"].write_text("printer.home.arpa\n")
    hosts(box)
    box["log"].unlink()
    assert hosts(box).returncode == 0
    assert not box["log"].exists(), "no nginx test, no reload for an unchanged list"


def test_a_map_nginx_refuses_is_rolled_back(box):
    box["src"].write_text("old.lan\n")
    assert hosts(box).returncode == 0
    box["src"].write_text("new.lan\n")
    result = hosts(box, {"NGINX_EXIT": "1"})
    assert result.returncode == 1 and "previous ones are kept" in result.stderr
    assert entries(box) == ['"old.lan" 1;']
    assert "reload" not in box["log"].read_text().split("nginx -t -q")[-1]


def test_the_number_of_names_is_bounded(box):
    box["src"].write_text("".join(f"host{i}.lan\n" for i in range(40)))
    result = hosts(box)
    assert result.returncode == 0 and len(entries(box)) == 32 and "skipped" in result.stderr


def test_the_test_hooks_do_not_exist_for_root():
    text = HELPER.read_text()
    assert "_TESTING = os.geteuid() != 0" in text
    for hook in ("EITRI_HOSTS_ROOT", "EITRI_HOSTS_NGINX", "EITRI_HOSTS_SYSTEMCTL"):
        assert f'_TESTING and os.environ.get("{hook}")' in text, hook
