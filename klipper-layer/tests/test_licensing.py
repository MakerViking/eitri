"""Licence tags: extras are GPL-3.0-or-later, the config files adapted from
QIDI's configuration are AGPL-3.0-or-later with an attribution line, the rest
of the layer follows the repository (Apache-2.0)."""
import re

import pytest

from conftest import CONFIG, EXTRAS, LAYER, extra_files

AGPL = "AGPL-3.0-or-later"
ADAPTED = ["printer.cfg.tmpl", "macros.cfg", "eitri-variables.toml"]
EITRI_WRITTEN = ["flavors/klipper.cfg", "flavors/kalico.cfg"]


def spdx(path):
    found = re.findall(r"^#\s*SPDX-License-Identifier:\s*(\S+)",
                       path.read_text(), re.M)
    assert len(found) == 1, "%s needs exactly one SPDX tag" % path
    return found[0]


# Derived from Klipper's heater_fan.py, which is GPL-3.0-only: the file says so
# itself and cannot be "or later".
GPL_ONLY = {"qd_chamber_fan.py"}


@pytest.mark.parametrize("path", extra_files(), ids=lambda p: p.name)
def test_every_extra_is_gpl_or_later(path):
    want = "GPL-3.0-only" if path.name in GPL_ONLY else "GPL-3.0-or-later"
    assert spdx(path) == want


@pytest.mark.parametrize("name", ADAPTED)
def test_adapted_config_files_are_agpl_with_attribution(name):
    path = CONFIG / name
    assert spdx(path) == AGPL
    head = "\n".join(path.read_text().splitlines()[:12])
    assert "QIDITECH/QIDI_PLUS4" in head
    assert "Plus4_v1.7.1" in head


@pytest.mark.parametrize("name", EITRI_WRITTEN)
def test_eitri_written_config_files_keep_the_repo_licence(name):
    assert spdx(CONFIG / name) == "Apache-2.0"


def test_every_config_file_is_tagged():
    """A new config file must pick a licence on purpose."""
    tagged = set(ADAPTED) | set(EITRI_WRITTEN)
    files = {p.relative_to(CONFIG).as_posix() for p in CONFIG.rglob("*")
             if p.is_file() and p.suffix in (".cfg", ".tmpl", ".toml")}
    assert files == tagged


def test_license_text_and_note_are_present():
    text = (CONFIG / "LICENSE").read_text()
    assert "GNU AFFERO GENERAL PUBLIC LICENSE" in text
    assert "Version 3, 19 November 2007" in text
    assert "END OF TERMS AND CONDITIONS" in text
    note = (CONFIG / "LICENSE-NOTE.md").read_text()
    for name in ADAPTED + EITRI_WRITTEN:
        assert name in note, name
    assert "QIDITECH/QIDI_PLUS4" in note and "AGPL-3.0-or-later" in note


def test_readmes_state_the_licences():
    cfg = (CONFIG / "README.md").read_text()
    assert "AGPL-3.0-or-later" in cfg and "LICENSE-NOTE.md" in cfg
    assert "Licence note for review" not in cfg          # the open question is closed
    top = (LAYER / "README.md").read_text()
    assert "AGPL-3.0-or-later" in top and "GPL-3.0-or-later" in top


def test_extras_licence_file_is_unchanged_gpl():
    assert "GNU GENERAL PUBLIC LICENSE" in (EXTRAS / "LICENSE").read_text()
