"""overlay-install.sh: symlink the extras into a Klipper/Kalico clone without
ever dirtying it, idempotently, and remove exactly what it added."""
import os
import subprocess


from conftest import CONFIG, EXTRAS, PYTHON, SCRIPTS, git

INSTALL = SCRIPTS / "overlay-install.sh"


def install(clone, printer_data, *extra, check=True):
    res = subprocess.run(
        ["bash", str(INSTALL), "--klipper-dir", str(clone),
         "--printer-data", str(printer_data),
         "--flavor-file", str(printer_data / "no-such-flavor-file"), *extra],
        capture_output=True, text=True,
        env={**os.environ, "EITRI_PYTHON": PYTHON})
    if check:
        assert res.returncode == 0, res.stdout + res.stderr
    return res


def status(clone):
    return git("status", "--porcelain=v1", "--untracked-files=all",
               cwd=clone)


def provided():
    return sorted(p.name for p in EXTRAS.glob("*.py"))


def linked(clone):
    d = clone / "klippy" / "extras"
    return sorted(p.name for p in d.iterdir()
                  if p.is_symlink()
                  and os.readlink(p).startswith(str(EXTRAS)))


def test_install_keeps_tree_clean(clone, flavor, tmp_path):
    pd = tmp_path / "pd"
    assert status(clone) == ""
    res = install(clone, pd)
    assert status(clone) == ""
    names = linked(clone)
    assert names, "nothing was linked"
    # every provided extra is linked unless the clone already tracks that name
    for name in provided():
        tracked = git("ls-files", f"klippy/extras/{name}", cwd=clone).strip()
        if tracked:
            assert name not in names
            assert f"skip   {name}" in res.stdout
        else:
            assert name in names
    # the symlinks resolve to the real files
    for name in names:
        assert (clone / "klippy" / "extras" / name).resolve() \
            == (EXTRAS / name).resolve()
    # hidden through .git/info/exclude, not .gitignore
    exclude = (clone / ".git" / "info" / "exclude").read_text()
    for name in names:
        assert f"/klippy/extras/{name}" in exclude
    assert git("status", "--porcelain", "--ignored", cwd=clone) \
        .count("!! klippy/extras/") >= len(names)


def test_flavor_specific_behaviour(clone, flavor, tmp_path):
    pd = tmp_path / "pd"
    res = install(clone, pd)
    assert f"({flavor} clone" in res.stdout
    shell = clone / "klippy" / "extras" / "gcode_shell_command.py"
    if flavor == "kalico":
        # Kalico tracks its own gcode_shell_command: never shadowed
        assert not shell.is_symlink()
        assert "gcode_shell_command.py (tracked in this clone" in res.stdout
    else:
        assert shell.is_symlink()
    link = pd / "config" / "eitri-flavor.cfg"
    assert os.readlink(link) == f"eitri/flavors/{flavor}.cfg"
    assert link.resolve() == (CONFIG / "flavors" / f"{flavor}.cfg").resolve()
    assert (pd / "config" / "eitri").resolve() == CONFIG.resolve()


def test_idempotent(clone, tmp_path):
    pd = tmp_path / "pd"
    install(clone, pd)
    first = (sorted(os.listdir(clone / "klippy" / "extras")),
             (clone / ".git" / "info" / "exclude").read_text(),
             os.readlink(pd / "config" / "eitri"))
    again = install(clone, pd)
    second = (sorted(os.listdir(clone / "klippy" / "extras")),
              (clone / ".git" / "info" / "exclude").read_text(),
              os.readlink(pd / "config" / "eitri"))
    assert first == second
    assert status(clone) == ""
    assert "already linked" in again.stdout
    exclude = second[1]
    assert exclude.count(">>> eitri klipper-layer") == 1


def test_uninstall_removes_only_ours(clone, tmp_path):
    pd = tmp_path / "pd"
    before_exclude = (clone / ".git" / "info" / "exclude").read_text()
    before_files = sorted(os.listdir(clone / "klippy" / "extras"))
    install(clone, pd)
    # the user's own printer.cfg must survive an uninstall
    (pd / "config" / "printer.cfg").write_text("[mcu]\n")
    install(clone, pd, "--uninstall")
    assert sorted(os.listdir(clone / "klippy" / "extras")) == before_files
    assert (clone / ".git" / "info" / "exclude").read_text().strip() \
        == before_exclude.strip()
    assert status(clone) == ""
    assert not (pd / "config" / "eitri").exists()
    assert not (pd / "config" / "eitri-flavor.cfg").is_symlink()
    assert (pd / "config" / "printer.cfg").read_text() == "[mcu]\n"


def test_never_clobbers_foreign_files(clone, tmp_path):
    pd = tmp_path / "pd"
    mine = clone / "klippy" / "extras" / "qd_probe.py"
    mine.write_text("# somebody else's module\n")
    res = install(clone, pd, check=False)
    assert res.returncode == 3, res.stdout + res.stderr
    assert mine.read_text() == "# somebody else's module\n"
    assert not mine.is_symlink()
    assert "qd_chamber_fan.py" in linked(clone)   # the rest still installed
    assert "qd_probe.py" not in linked(clone)
    # the foreign file is untracked and visible in git status (it is theirs)
    assert "klippy/extras/qd_probe.py" in status(clone)


def test_never_shadows_tracked_files(clone, tmp_path):
    """Even a tracked file with one of our names is left untouched."""
    pd = tmp_path / "pd"
    target = clone / "klippy" / "extras" / "qd_zoffset_var.py"
    target.write_text("# tracked upstream\n")
    git("add", str(target), cwd=clone)
    git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "x",
        cwd=clone)
    before = status(clone)
    install(clone, pd)
    assert target.read_text() == "# tracked upstream\n"
    assert not target.is_symlink()
    assert status(clone) == before == ""


def test_dry_run_changes_nothing(clone, tmp_path):
    pd = tmp_path / "pd"
    res = install(clone, pd, "--dry-run")
    assert "[dry-run]" in res.stdout
    assert not (pd / "config").exists()
    assert linked(clone) == []
    assert "eitri klipper-layer" not in \
        (clone / ".git" / "info" / "exclude").read_text()


def test_stale_links_removed(clone, tmp_path):
    pd = tmp_path / "pd"
    install(clone, pd)
    ghost = clone / "klippy" / "extras" / "qd_removed.py"
    ghost.symlink_to(EXTRAS / "qd_removed.py")      # an extra we dropped
    with open(clone / ".git" / "info" / "exclude", "a") as f:
        f.write("/klippy/extras/qd_removed.py\n")
    assert status(clone) == ""
    install(clone, pd)
    assert not ghost.is_symlink()
    assert status(clone) == ""


def test_dirty_tree_is_reported_as_unchanged(clone, tmp_path):
    """A pre-existing local modification is not our doing and not an error."""
    pd = tmp_path / "pd"
    (clone / "README.md").write_text("local edit\n")
    before = status(clone)
    assert before.strip()
    install(clone, pd)
    assert status(clone) == before


def test_rejects_non_clone(tmp_path):
    res = subprocess.run(["bash", str(INSTALL), "--klipper-dir",
                          str(tmp_path)], capture_output=True, text=True)
    assert res.returncode == 1
    assert "not a Klipper/Kalico clone" in res.stderr


def test_init_config_renders_once(clone, tmp_path):
    pd = tmp_path / "pd"
    install(clone, pd, "--init-config")
    cfg = pd / "config" / "printer.cfg"
    assert cfg.is_file()
    assert "@@" not in cfg.read_text()
    cfg.write_text(cfg.read_text() + "\n# user edit\n")
    again = install(clone, pd, "--init-config")
    assert "user edit" in cfg.read_text()
    assert "user-owned" in again.stdout
    assert status(clone) == ""


def test_links_from_a_relocated_extras_dir_are_adopted(clone, tmp_path):
    """The installer may move the layer between releases; the old links
    (here dangling, as after the old release directory was deleted) are
    replaced, not reported as a conflict, and uninstall removes them."""
    pd = tmp_path / "pd"
    old = clone / "klippy" / "extras" / "qd_chamber_fan.py"
    old.symlink_to("/data/eitri/extras/qd_chamber_fan.py")
    old2 = clone / "klippy" / "extras" / "qd_zoffset_var.py"
    old2.symlink_to("/data/eitri/releases/1.2.3/extras/qd_zoffset_var.py")
    with open(clone / ".git" / "info" / "exclude", "a") as f:
        f.write("/klippy/extras/qd_chamber_fan.py\n"
                "/klippy/extras/qd_zoffset_var.py\n")
    assert status(clone) == ""
    install(clone, pd)                               # exit 0: no conflict
    assert old.resolve() == (EXTRAS / "qd_chamber_fan.py").resolve()
    assert old2.resolve() == (EXTRAS / "qd_zoffset_var.py").resolve()
    assert status(clone) == ""


def test_a_symlink_to_an_unrelated_place_is_foreign(clone, tmp_path):
    pd = tmp_path / "pd"
    foreign = clone / "klippy" / "extras" / "qd_plr.py"
    foreign.symlink_to("/opt/somebody/qd_plr.py")
    res = install(clone, pd, check=False)
    assert res.returncode == 3, res.stdout + res.stderr
    assert os.readlink(foreign) == "/opt/somebody/qd_plr.py"
