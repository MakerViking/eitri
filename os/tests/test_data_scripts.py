# SPDX-License-Identifier: Apache-2.0
"""grow-data, data-init and mark-good, run against files and stub commands."""
from __future__ import annotations

import os
import re
import stat
from pathlib import Path

import pytest
from conftest import LIBEXEC, need, run

MIB = 1 << 20


def make_disk(path: Path, total_mib: int, image_mib: int) -> None:
    """A GPT disk laid out like eitri-plus4.img, then enlarged like a bigger eMMC."""
    need("sfdisk", "truncate")
    path.write_bytes(b"")
    run(["truncate", "-s", f"{image_mib}M", path], check=True)
    table = (
        "label: gpt\n"
        "start=2048, size=16384, type=EBD0A0A2-B9E5-4433-87C0-68B6B72699C7, name=\"boot\"\n"
        "start=18432, size=32768, type=B921B045-1DF0-41C3-AF44-4C6F280D3FAE, name=\"rootfs-a\"\n"
        "start=51200, size=32768, type=B921B045-1DF0-41C3-AF44-4C6F280D3FAE, name=\"rootfs-b\"\n"
        "start=83968, size=8192, type=0FC63DAF-8483-4772-8E79-3D69D8477DE4, name=\"data\"\n"
    )
    run(["sfdisk", "--quiet", path], input=table, check=True)
    run(["truncate", "-s", f"{total_mib}M", path], check=True)


def partitions(path: Path) -> dict[str, tuple[int, int]]:
    dump = run(["sfdisk", "-d", path], check=True).stdout
    found = {}
    for line in dump.splitlines():
        m = re.search(r'start= *(\d+), size= *(\d+).*name="([^"]+)"', line)
        if m:
            found[m.group(3)] = (int(m.group(1)), int(m.group(2)))
    return found


def test_grow_data_fills_the_disk_and_moves_the_backup_gpt(tmp_path):
    disk = tmp_path / "emmc.img"
    make_disk(disk, total_mib=256, image_mib=64)
    before = partitions(disk)
    env = {**os.environ, "EITRI_DISK": str(disk), "EITRI_ROOT_DISK": str(disk)}
    result = run([LIBEXEC / "grow-data"], env=env)
    assert result.returncode == 0, result.stderr

    after = partitions(disk)
    assert after["boot"] == before["boot"] and after["rootfs-a"] == before["rootfs-a"]
    assert after["rootfs-b"] == before["rootfs-b"]
    start, size = after["data"]
    assert start == before["data"][0], "the partition must not move"
    last_usable = 256 * MIB // 512 - 34
    assert last_usable - (start + size - 1) < 2048, "data should reach the end of the disk"
    # The table was rewritten for the full disk: its header now knows the real size.
    dump = run(["sfdisk", "-d", disk], check=True).stdout
    assert f"last-lba: {last_usable}" in dump


def test_grow_data_is_idempotent(tmp_path):
    disk = tmp_path / "emmc.img"
    make_disk(disk, total_mib=256, image_mib=64)
    env = {**os.environ, "EITRI_DISK": str(disk), "EITRI_ROOT_DISK": str(disk)}
    run([LIBEXEC / "grow-data"], env=env)
    once = disk.read_bytes()[: 2 * MIB]
    result = run([LIBEXEC / "grow-data"], env=env)
    assert result.returncode == 0 and "already fills" in result.stdout
    assert disk.read_bytes()[: 2 * MIB] == once


def test_grow_data_leaves_a_foreign_table_alone(tmp_path):
    need("sfdisk", "truncate")
    disk = tmp_path / "other.img"
    run(["truncate", "-s", "64M", disk], check=True)
    run(["sfdisk", "--quiet", disk], input="label: gpt\nstart=2048, size=8192, name=\"stuff\"\n", check=True)
    before = disk.read_bytes()
    result = run([LIBEXEC / "grow-data"], env={**os.environ, "EITRI_DISK": str(disk), "EITRI_ROOT_DISK": str(disk)})
    assert result.returncode == 0 and "no partition named" in result.stdout
    assert disk.read_bytes() == before


def test_grow_data_never_fails_the_boot(tmp_path, cmdline):
    result = run([LIBEXEC / "grow-data"], env={**os.environ, "EITRI_CMDLINE": str(cmdline("quiet"))})
    assert result.returncode == 0
    result = run([LIBEXEC / "grow-data"], env={**os.environ, "EITRI_DISK": str(tmp_path / "missing")})
    assert result.returncode == 0


def test_grow_data_never_touches_a_disk_that_does_not_hold_the_root_file_system(tmp_path):
    """eitri.disk= pointing at another disk (e.g. the eMMC while / is on a microSD card) changes nothing."""
    disk = tmp_path / "emmc.img"
    make_disk(disk, total_mib=256, image_mib=64)
    before = disk.read_bytes()
    for root in (str(tmp_path / "sdcard.img"), ""):
        env = {**os.environ, "EITRI_DISK": str(disk), "EITRI_ROOT_DISK": root}
        if not root:
            env.pop("EITRI_ROOT_DISK")
            env["EITRI_CMDLINE"] = str(tmp_path / "missing-cmdline")
        result = run([LIBEXEC / "grow-data"], env=env)
        assert result.returncode == 0 and "leaving" in result.stdout, result.stdout
        assert disk.read_bytes() == before


# --- grow-data on a DOS disk started by the in-place boot.scr (the bench microSD image) ----------

LABEL_ID = "e17a5d01"


def make_dos_disk(path: Path, total_mib: int, image_mib: int, label_id: str = LABEL_ID, data_nr: int = 7) -> None:
    """Laid out like os/bench/mk-sd-bench.sh: p1 vfat, p3 extended, p5/p6 rootfs slots, p7 data last."""
    need("sfdisk", "truncate")
    path.write_bytes(b"")
    run(["truncate", "-s", f"{image_mib}M", path], check=True)
    end = image_mib * 2048
    table = (
        f"label: dos\nlabel-id: 0x{label_id}\nunit: sectors\n\n"
        "1 : start=32768, size=32768, type=c, bootable\n"
        f"3 : start=65536, size={end - 65536}, type=5\n"
        "5 : start=67584, size=16384, type=83\n"
        "6 : start=86016, size=16384, type=83\n"
    )
    if data_nr == 7:
        table += f"7 : start=104448, size={end - 104448}, type=83\n"
    run(["sfdisk", "--quiet", path], input=table, check=True)
    run(["truncate", "-s", f"{total_mib}M", path], check=True)


def dos_parts(path: Path) -> dict[int, tuple[int, int]]:
    out = {}
    for line in run(["sfdisk", "-d", path], check=True).stdout.splitlines():
        m = re.match(r"\S*?(\d+) : start= *(\d+), size= *(\d+)", line)
        if m:
            out[int(m.group(1))] = (int(m.group(2)), int(m.group(3)))
    return out


def dos_env(tmp_path, cmdline, disk, line=None, root_disk=None):
    line = line or (f"root=PARTUUID={LABEL_ID}-05 ro rootwait eitri.slot=A "
                    f"eitri.data=PARTUUID={LABEL_ID}-07 eitri.bootfs=PARTUUID={LABEL_ID}-01 eitri.growdata=1")
    return {**os.environ, "EITRI_DISK": str(disk), "EITRI_ROOT_DISK": str(root_disk or disk), "EITRI_CMDLINE": str(cmdline(line))}


def test_grow_data_enlarges_the_extended_and_the_last_logical_partition(tmp_path, cmdline):
    disk = tmp_path / "sdcard.img"
    make_dos_disk(disk, total_mib=512, image_mib=128)
    before = dos_parts(disk)
    result = run([LIBEXEC / "grow-data"], env=dos_env(tmp_path, cmdline, disk))
    assert result.returncode == 0 and "growing extended partition 3 and partition 7" in result.stdout, result.stdout
    after = dos_parts(disk)
    total = 512 * 2048
    assert after[1] == before[1] and after[5] == before[5] and after[6] == before[6]
    assert after[3][0] == before[3][0] and sum(after[3]) == total
    assert after[7][0] == before[7][0] and sum(after[7]) == total
    dump = run(["sfdisk", "-d", disk], check=True).stdout
    assert f"label-id: 0x{LABEL_ID}" in dump and "bootable" in dump
    again = run([LIBEXEC / "grow-data"], env=dos_env(tmp_path, cmdline, disk))
    assert "already fills" in again.stdout and dos_parts(disk) == after


@pytest.mark.parametrize("case", ["no-opt-in", "other-label-id", "root-elsewhere", "root-other-disk", "not-last", "gpt-spec"])
def test_grow_data_leaves_dos_disks_alone_unless_every_check_passes(tmp_path, cmdline, case):
    disk = tmp_path / "disk.img"
    make_dos_disk(disk, total_mib=256, image_mib=128, label_id="6a783df4" if case == "other-label-id" else LABEL_ID)
    line = None
    root_disk = None
    if case == "no-opt-in":
        line = f"root=PARTUUID={LABEL_ID}-05 eitri.data=PARTUUID={LABEL_ID}-07"
    elif case == "root-elsewhere":
        line = f"root=PARTUUID=6a783df4-02 eitri.data=PARTUUID={LABEL_ID}-07 eitri.growdata=1"
    elif case == "root-other-disk":
        root_disk = tmp_path / "emmc.img"
    elif case == "not-last":
        line = f"root=PARTUUID={LABEL_ID}-05 eitri.data=PARTUUID={LABEL_ID}-05 eitri.growdata=1"
    elif case == "gpt-spec":
        line = f"root=PARTUUID={LABEL_ID}-05 eitri.data=LABEL=eitri-data eitri.growdata=1"
    before = disk.read_bytes()
    result = run([LIBEXEC / "grow-data"], env=dos_env(tmp_path, cmdline, disk, line, root_disk))
    assert result.returncode == 0, result.stderr
    assert "growing" not in result.stdout, result.stdout
    assert disk.read_bytes() == before


# --- data-init ----------------------------------------------------------------

@pytest.fixture
def data_env(tmp_path):
    need("install")
    factory = tmp_path / "factory"
    (factory / "camera").mkdir(parents=True)
    (factory / "moonraker.asvc").write_text("klipper\nmoonraker\n")
    (factory / "camera/go2rtc.yaml").write_text("api:\n")
    (factory / "camera/camera.env.example").write_text("#EITRI_CAMERA_DEVICE=\n")
    bootfs = tmp_path / "bootfs"
    bootfs.mkdir()
    user = f"{os.getuid()}:{os.getgid()}"
    return {
        **os.environ,
        "EITRI_DATA": str(tmp_path / "data"),
        "EITRI_FACTORY": str(factory),
        "EITRI_BOOTFS": str(bootfs),
        "EITRI_OWNER": user,
        "EITRI_NO_RESIZE": "1",
        "EITRI_CONFIGURATOR_SRC": str(tmp_path / "image-configurator"),
    }


def test_data_init_builds_the_whole_layout(data_env):
    data = Path(data_env["EITRI_DATA"])
    data.mkdir()
    Path(data_env["EITRI_CONFIGURATOR_SRC"]).mkdir()
    result = run([LIBEXEC / "data-init"], env=data_env)
    assert result.returncode == 0, result.stderr
    for rel in [
        "firmware", "venv/klippy", "venv/moonraker",
        "printer_data/config", "printer_data/logs", "printer_data/gcodes", "printer_data/database",
        "printer_data/comms", "eitri/extras", "eitri/klarity", "eitri/toolchain", "eitri/home",
        "eitri/state/etc/upper", "eitri/state/etc/work",
        "eitri/logs",  # eitri-prepare and eitri-verify write update.jsonl here
    ]:
        assert (data / rel).is_dir(), rel
    assert (data / "printer_data/moonraker.asvc").read_text() == "klipper\nmoonraker\n"
    assert (data / "printer_data/config/go2rtc.yaml").is_file()
    assert (data / "eitri/ui").is_dir() and not (data / "eitri/ui").is_symlink()
    assert os.readlink(data / "eitri/ui/current") == "/usr/share/eitri/ui/fluidd"        # nginx root is eitri/ui/current
    assert (data / "eitri/channel").read_text() == "eitri\n" and not (data / "eitri/flavor").exists()   # flavor is the seed's
    assert (data / "eitri/state/layout-version").read_text().strip() == "1"
    assert (data / "eitri/state/first-boot").is_file()
    assert stat.S_IMODE((data / "eitri/home").stat().st_mode) == 0o750


def test_data_init_converts_the_old_ui_link_and_keeps_existing_flavor_and_channel(data_env):
    data = Path(data_env["EITRI_DATA"])
    (data / "eitri").mkdir(parents=True)
    Path(data_env["EITRI_CONFIGURATOR_SRC"]).mkdir()
    os.symlink("/usr/share/eitri/ui/mainsail", data / "eitri/ui")            # layout of an older image
    (data / "eitri/flavor").write_text("kalico\n")
    (data / "eitri/channel").write_text("upstream\n")
    result = run([LIBEXEC / "data-init"], env=data_env, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert (data / "eitri/ui").is_dir() and os.readlink(data / "eitri/ui/current") == "/usr/share/eitri/ui/mainsail"
    assert (data / "eitri/flavor").read_text() == "kalico\n" and (data / "eitri/channel").read_text() == "upstream\n"
    again = run([LIBEXEC / "data-init"], env=data_env, capture_output=True, text=True)
    assert again.returncode == 0 and os.readlink(data / "eitri/ui/current") == "/usr/share/eitri/ui/mainsail"


def test_data_init_creates_the_configurator_venv_from_the_image_copy(data_env, tmp_path):
    src = tmp_path / "image-configurator"
    src.mkdir()
    data = Path(data_env["EITRI_DATA"])
    result = run([LIBEXEC / "data-init"], env=data_env)
    assert result.returncode == 0, result.stderr
    venv = data / "eitri/configurator/venv"
    assert (venv / "bin/python").exists()
    cfg = (venv / "pyvenv.cfg").read_text()
    assert "include-system-site-packages = true" in cfg
    pth = next(venv.glob("lib/python3*/site-packages/eitri-image.pth"))
    assert pth.read_text().strip() == str(src)
    marker = venv / "marker"
    marker.write_text("kept")
    run([LIBEXEC / "data-init"], env=data_env, check=True)
    assert marker.read_text() == "kept", "an existing environment is never replaced"


def test_data_init_never_overwrites_user_files(data_env):
    data = Path(data_env["EITRI_DATA"])
    (data / "printer_data").mkdir(parents=True)
    (data / "printer_data/moonraker.asvc").write_text("mine\n")
    run([LIBEXEC / "data-init"], env=data_env, check=True)
    run([LIBEXEC / "data-init"], env=data_env, check=True)
    assert (data / "printer_data/moonraker.asvc").read_text() == "mine\n"
    first = (data / "eitri/state/first-boot").read_text()
    run([LIBEXEC / "data-init"], env=data_env, check=True)
    assert (data / "eitri/state/first-boot").read_text() == first, "only the first boot stamps the date"


def test_data_init_installs_ssh_keys_once_and_rejects_junk(data_env):
    data = Path(data_env["EITRI_DATA"])
    keys = Path(data_env["EITRI_BOOTFS"]) / "authorized_keys"
    good = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIexample user@bench"
    keys.write_text(f"# a comment\n{good}\nnot a key\n$(rm -rf /)\n")
    run([LIBEXEC / "data-init"], env=data_env, check=True)
    run([LIBEXEC / "data-init"], env=data_env, check=True)
    target = data / "eitri/home/.ssh/authorized_keys"
    assert target.read_text() == good + "\n"
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert stat.S_IMODE(target.parent.stat().st_mode) == 0o700


def test_data_init_skips_an_unmounted_data_partition(tmp_path):
    need("mountpoint")
    env = {k: v for k, v in os.environ.items() if not k.startswith("EITRI_")}
    # The real /data is not a mount point on a development machine.
    if Path("/data").is_mount():
        pytest.skip("/data is a mount point here")
    env["EITRI_RUN"] = str(tmp_path / "run")
    result = run([LIBEXEC / "data-init"], env=env)
    assert result.returncode == 0 and "not mounted" in result.stdout
    assert (tmp_path / "run/degraded").is_file()


# --- mark-good ----------------------------------------------------------------

@pytest.fixture
def stubs(tmp_path):
    """Stub systemctl, mountpoint, sleep and rauc, controlled by files."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    calls = tmp_path / "calls"
    inactive = tmp_path / "inactive"
    inactive.write_text("")
    # is-active --quiet UNIT: fails for the units listed in "inactive". show -p PROP --value UNIT: ActiveState
    # follows the same file, Result comes from "results" (lines "unit=value", default success).
    results = tmp_path / "results"
    results.write_text("")
    (bin_dir / "systemctl").write_text(
        "#!/bin/sh\n"
        'if [ "$1" = show ]; then\n'
        '  case "$3" in\n'
        f'  ActiveState) if grep -qx "$5" "{inactive}"; then echo inactive; else echo active; fi ;;\n'
        f'  Result) sed -n "s/^$5=//p" "{results}" | head -n 1 | grep . || echo success ;;\n'
        '  esac\n'
        '  exit 0\n'
        'fi\n'
        f'shift; ! grep -qx "$2" "{inactive}"\n')
    (bin_dir / "mountpoint").write_text("#!/bin/sh\nexit 0\n")
    (bin_dir / "sleep").write_text("#!/bin/sh\nexit 0\n")
    (bin_dir / "rauc").write_text(f'#!/bin/sh\necho "$@" >> "{calls}"\n')
    for f in bin_dir.iterdir():
        f.chmod(0o755)
    return {"bin": bin_dir, "calls": calls, "inactive": inactive, "results": results}


def mark_good(stubs, cmdline_path, timeout="6", *args, update_bin=None):
    # By default no eitri-update exists (an image without it), so mark-good is the committer.
    env = {**os.environ, "PATH": f"{stubs['bin']}:{os.environ['PATH']}", "EITRI_CMDLINE": str(cmdline_path),
           "EITRI_TIMEOUT": timeout, "EITRI_UPDATE_BIN": str(update_bin or stubs["bin"] / "no-eitri-update")}
    return run([LIBEXEC / "mark-good", *args], env=env)


def test_mark_good_when_healthy(stubs, cmdline):
    result = mark_good(stubs, cmdline("eitri.disk=/dev/mmcblk1 rauc.slot=B"))
    assert result.returncode == 0, result.stderr
    assert stubs["calls"].read_text().strip() == "status mark-good"


def test_mark_good_waits_then_gives_up_when_a_service_is_down(stubs, cmdline):
    stubs["inactive"].write_text("nginx.service\n")
    result = mark_good(stubs, cmdline("eitri.disk=/dev/mmcblk1 rauc.slot=B"))
    assert result.returncode != 0
    assert "nginx.service is not active" in result.stderr
    assert not stubs["calls"].exists()


def test_mark_good_does_nothing_on_a_bench_sd_boot(stubs, cmdline):
    result = mark_good(stubs, cmdline("eitri.disk=/dev/mmcblk0 rauc.slot=A"))
    assert result.returncode == 0 and not stubs["calls"].exists()


def test_mark_good_defers_to_rauc_when_another_boot_manager_started_the_system(stubs, cmdline):
    result = mark_good(stubs, cmdline("root=PARTUUID=6a783df4-05 ro eitri.slot=A"))
    assert result.returncode == 0, result.stderr
    assert stubs["calls"].read_text().strip() == "status mark-good"


def test_mark_good_leaves_the_commit_to_eitri_verify_when_eitri_update_is_installed(stubs, cmdline, tmp_path):
    # Committing before eitri-verify has looked at Klipper, Moonraker and the MCUs would refill the boot
    # attempts on every boot and make the fall-back unreachable.
    update = tmp_path / "eitri-update"
    update.write_text("#!/bin/sh\n")
    update.chmod(0o755)
    result = mark_good(stubs, cmdline("eitri.disk=/dev/mmcblk1 rauc.slot=B"), "6", update_bin=update)
    assert result.returncode == 0, result.stderr
    assert "commits the slot" in result.stdout
    assert not stubs["calls"].exists()


def test_mark_good_check_gates_without_committing(stubs, cmdline, tmp_path):
    update = tmp_path / "eitri-update"
    update.write_text("#!/bin/sh\n")
    update.chmod(0o755)
    line = cmdline("eitri.disk=/dev/mmcblk1 rauc.slot=B")
    result = mark_good(stubs, line, "6", "--check", update_bin=update)
    assert result.returncode == 0, result.stderr
    assert not stubs["calls"].exists(), "--check must never touch the boot state"
    stubs["inactive"].write_text("sshd.service\n")
    result = mark_good(stubs, line, "6", "--check", update_bin=update)
    assert result.returncode != 0 and "sshd.service is not active" in result.stderr
    assert not stubs["calls"].exists()


def test_mark_good_rejects_unknown_arguments(stubs, cmdline):
    result = mark_good(stubs, cmdline("eitri.disk=/dev/mmcblk1"), "6", "--force")
    assert result.returncode == 2 and not stubs["calls"].exists()


def test_mark_good_accepts_a_configurator_that_retired_after_setup(stubs, cmdline, tmp_path):
    # Decision 8: the configurator exits cleanly once setup is complete. That must not fail the health gate, or
    # every boot after setup would count as bad and every update would roll back.
    (tmp_path / "image-configurator").mkdir()
    line = cmdline("eitri.disk=/dev/mmcblk1 rauc.slot=B")
    env = {"EITRI_CONFIGURATOR_SRC": str(tmp_path / "image-configurator")}

    def check():
        full = {**os.environ, "PATH": f"{stubs['bin']}:{os.environ['PATH']}", "EITRI_CMDLINE": str(line), "EITRI_TIMEOUT": "6",
                "EITRI_UPDATE_BIN": str(stubs["bin"] / "none"), **env}
        return run([LIBEXEC / "mark-good", "--check"], env=full)

    stubs["inactive"].write_text("eitri-configurator.service\n")
    assert check().returncode == 0, "stopped after a clean exit is healthy"
    stubs["results"].write_text("eitri-configurator.service=exit-code\n")
    result = check()
    assert result.returncode != 0 and "eitri-configurator.service is not active" in result.stderr, "a crash is not"
    stubs["results"].write_text("eitri-configurator.service=success\n")
    stubs["inactive"].write_text("eitri-configurator.service\nnginx.service\n")
    assert check().returncode != 0, "every other unit must still be active"


def test_data_init_needs_no_stat_applet(data_env, tmp_path):
    # The image's BusyBox has no stat; the first boot of a converted copy of the real eMMC logged "stat: command not found"
    # from data-init (and left the seed's parent directories owned by root).
    stub = tmp_path / "nostat"
    stub.mkdir()
    (stub / "stat").write_text("#!/bin/sh\necho 'stat: command not found' >&2\nexit 127\n")
    (stub / "stat").chmod(0o755)
    env = dict(data_env, PATH=f"{stub}:{data_env['PATH']}")
    data = Path(env["EITRI_DATA"])
    (data / "firmware").mkdir(parents=True)
    Path(env["EITRI_CONFIGURATOR_SRC"]).mkdir()
    result = run([LIBEXEC / "data-init"], env=env)
    assert result.returncode == 0, result.stderr
    assert "stat" not in result.stderr, result.stderr
