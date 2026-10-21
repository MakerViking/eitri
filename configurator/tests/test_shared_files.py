# SPDX-License-Identifier: Apache-2.0
"""/data/eitri belongs to root (sshd and the root helpers under eitri/state depend on it), so the configurator, which runs as
the service user, must not need to create files in it: flavor and channel are rewritten in place, the web UI choice is a
symlink inside the service user's own eitri/ui directory, the hostname memo lives in eitri/configurator."""
import os
import stat

import pytest

from eitri_configurator.state import set_symlink, write_shared

pytestmark = pytest.mark.skipif(os.geteuid() == 0, reason="root ignores directory permissions")


def test_write_shared_rewrites_in_place_when_the_directory_is_not_ours(tmp_path):
    d = tmp_path / "eitri"
    d.mkdir()
    f = d / "flavor"
    f.write_text("klipper\n")
    inode = f.stat().st_ino
    d.chmod(0o555)                                     # the directory refuses new files, as /data/eitri does
    try:
        write_shared(f, "kalico\n")
        assert f.read_text() == "kalico\n" and f.stat().st_ino == inode
        with pytest.raises(PermissionError):           # but it does not create a file it cannot place
            write_shared(d / "channel", "eitri\n")
    finally:
        d.chmod(0o755)


def test_write_shared_is_atomic_when_it_can_be(tmp_path):
    f = tmp_path / "flavor"
    f.write_text("klipper\n")
    inode = f.stat().st_ino
    write_shared(f, "kalico\n")
    assert f.read_text() == "kalico\n" and f.stat().st_ino != inode and stat.S_IMODE(f.stat().st_mode) == 0o644


def test_set_symlink_replaces_the_target_atomically(tmp_path):
    set_symlink(tmp_path / "current", "/usr/share/eitri/ui/fluidd")
    set_symlink(tmp_path / "current", "/usr/share/eitri/ui/mainsail")
    assert os.readlink(tmp_path / "current") == "/usr/share/eitri/ui/mainsail"
    assert [p.name for p in tmp_path.iterdir()] == ["current"]


def test_apply_works_with_a_root_owned_eitri_dir(detected):
    s = detected
    eitri = s.settings.eitri_dir
    (eitri / "ui").mkdir(exist_ok=True)
    for name in ("flavor", "channel"):
        (eitri / name).write_text("x\n" if name == "channel" else "klipper\n")
    (eitri / "channel").write_text("eitri\n")
    s.put("/api/state", {"flavor": "kalico"})
    s.put("/api/options", {"ui": "mainsail"})
    eitri.chmod(0o555)
    try:
        j = s.job(s.post("/api/apply", {"confirm": True}))
        assert j["status"] == "done", j
    finally:
        eitri.chmod(0o755)
    assert (eitri / "flavor").read_text() == "kalico\n" and (eitri / "channel").read_text() == "eitri\n"
    assert os.readlink(eitri / "ui" / "current") == "/usr/share/eitri/ui/mainsail"
    assert s.settings.hostname_file.parent == s.settings.configurator_dir
