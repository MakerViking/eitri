#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Repository-wide checks that need no hardware and no network.

    python3 -I tools/check-repo.py

* release.lock parses and every pin is well formed (40-hex commits, 64-hex digests);
* LICENSE is the canonical Apache-2.0 text;
* relative markdown links and anchors resolve;
* .gitignore keeps build output, firmware, vendor assets and secrets out, and keeps the docs in;
* the clean-room boundary: no vendor binaries or display projects in the tree, no printer
  addresses, tokens or leftover redaction markers in text files;
* the shared contract: ports, service names, paths and the flavor file agree across components.
"""
from __future__ import annotations

import re
import subprocess
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
failures: list[str] = []
passed = 0


def check(cond: bool, msg: str) -> None:
    global passed
    if cond:
        passed += 1
    else:
        failures.append(msg)
        print("FAIL", msg)


def tracked_text_files() -> list[Path]:
    """Every file git would add (tracked or untracked-but-not-ignored)."""
    out = subprocess.run(["git", "-C", str(ROOT), "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
                         capture_output=True, check=True).stdout.decode()
    files = []
    for rel in filter(None, out.split("\0")):
        p = ROOT / rel
        if p.is_file() and not p.is_symlink():
            files.append(p)
    return files


FILES = tracked_text_files()


def is_text(p: Path) -> bool:
    return p.suffix.lower() not in {".png", ".woff2", ".woff", ".ttf", ".otf", ".ico", ".gz", ".zst", ".bin", ".pyc"}


# 1. release.lock ---------------------------------------------------------------------------------
lock = tomllib.loads((ROOT / "release.lock").read_text())
check(lock.get("schema") == 1, "release.lock schema = 1")
sha40 = re.compile(r"^[0-9a-f]{40}$")
sha256 = re.compile(r"^[0-9a-f]{64}$")
for group in ("os", "firmware", "camera", "ui", "attributed"):
    for name, entry in lock.get(group, {}).items():
        label = f"{group}.{name}"
        if "commit" in entry:
            check(bool(sha40.match(entry["commit"])), f"release.lock {label}: commit is 40-hex")
        if "sha256" in entry:
            check(bool(sha256.match(entry["sha256"])), f"release.lock {label}: sha256 is 64-hex")
        if group != "attributed":
            check("version" in entry and "source" in entry, f"release.lock {label}: version and source")
            check(entry.get("tested") is False, f"release.lock {label}: not claimed tested (pre-alpha)")
for name in ("katapult", "moonraker", "uv", "python"):
    check(name in lock, f"release.lock has [{name}]")
for comp in ("camera", "klarity", "klipper-extras", "klipper-layer", "installer", "configurator"):
    check(comp in lock.get("components", {}), f"release.lock lists component {comp}")
    src = lock["components"].get(comp, {}).get("source", "")
    if src.startswith("eitri/"):
        check((ROOT / src[len("eitri/"):]).exists(), f"release.lock component {comp}: {src} exists")

# 2. LICENSE ---------------------------------------------------------------------------------------
lic = (ROOT / "LICENSE").read_text()
check("Apache License" in lic and "Version 2.0, January 2004" in lic, "LICENSE is Apache-2.0")
check("END OF TERMS AND CONDITIONS" in lic and "APPENDIX: How to apply the Apache License" in lic, "LICENSE has terms and appendix")
check("GNU GENERAL PUBLIC LICENSE" in (ROOT / "klipper-layer/extras/LICENSE").read_text(), "klipper-layer/extras/LICENSE is the GPL")

# 3. markdown links and anchors --------------------------------------------------------------------
link_re = re.compile(r"\]\(([^)\s]+)\)")


def slug(title: str) -> str:
    return re.sub(r"[^\w\- ]", "", title.strip().lower()).replace(" ", "-")


def anchors(path: Path) -> set[str]:
    return {slug(m.group(1)) for line in path.read_text().splitlines() if (m := re.match(r"^#+\s+(.*)$", line))}


for md in (f for f in FILES if f.suffix == ".md"):
    in_fence = False
    for n, line in enumerate(md.read_text().splitlines(), 1):
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
        if in_fence:
            continue
        for target in link_re.findall(line):
            if target.startswith(("http://", "https://", "mailto:")):
                continue
            file_part, _, anchor = target.partition("#")
            dest = (md.parent / file_part).resolve() if file_part else md
            rel = md.relative_to(ROOT)
            if str(dest).startswith(str(ROOT.parent)) and not str(dest).startswith(str(ROOT)):
                continue  # points at the private project notes outside the repository
            check(dest.exists(), f"{rel}:{n} link -> {target}")
            if anchor and dest.exists() and dest.suffix == ".md":
                check(anchor in anchors(dest), f"{rel}:{n} anchor #{anchor} in {dest.name}")

# 4. .gitignore -------------------------------------------------------------------------------------
def ignored(path: str) -> bool:
    return subprocess.run(["git", "-C", str(ROOT), "check-ignore", "-q", "--no-index", path]).returncode == 0


for path in ["build/x.img", "out/a", "a.tft", "x.HMI", "secrets/token", "backups/a.img", "config.mksini", ".env",
             "a.bin", "klarity/__pycache__/x.pyc", "node_modules/x", "venv/bin/python", ".venv/x", "klarity/dist/x.whl",
             "x.egg-info/PKG-INFO", ".pytest_cache/x", ".ruff_cache/x"]:
    check(ignored(path), f".gitignore ignores {path}")
for path in ["release.lock", "README.md", "LICENSE", "NOTICE", "docs/ARCHITECTURE.md", ".editorconfig", "Makefile", "STATUS.md",
             "configurator/screenshots/desktop/01-welcome.png", "klarity/src/klarity/maps/default.yaml"]:
    check(not ignored(path), f".gitignore does not ignore {path}")

# 5. clean-room and secrets -------------------------------------------------------------------------
forbidden_suffix = {".tft", ".hmi", ".so", ".elf", ".img", ".raucb", ".pyx"}
for f in FILES:
    check(f.suffix.lower() not in forbidden_suffix, f"no vendor/binary artifact in the tree: {f.relative_to(ROOT)}")
secret = re.compile(r"192\.168\.0\.(76|162)\b|\bBearer [A-Za-z0-9._-]{16,}|\bREDACTED\b|-----BEGIN (RSA |EC |OPENSSH )?PRIVATE KEY-----")
for f in (f for f in FILES if is_text(f) and f.name != "check-repo.py"):
    try:
        text = f.read_text()
    except UnicodeDecodeError:
        continue
    for n, line in enumerate(text.splitlines(), 1):
        if secret.search(line) and not (f.suffix == ".py" and "tests" in f.parts):
            check(False, f"{f.relative_to(ROOT)}:{n} looks like a printer address, token or key: {line.strip()[:60]}")
check(True, "secret scan finished")

# 6. cross-component contract ------------------------------------------------------------------------
def read(rel: str) -> str:
    return (ROOT / rel).read_text()


check("7125" in read("klarity/config/klarity.cfg"), "klarity talks to Moonraker on 7125")
check("8080/snapshot" in read("klarity/config/klarity.cfg"), "klarity snapshot_url is ustreamer :8080/snapshot")
check("127.0.0.1:8080" in read("camera/moonraker/nginx-webcam.inc"), "nginx proxies /webcam/ to ustreamer :8080")
check("--port 8099" in read("configurator/systemd/eitri-configurator.service"), "configurator unit uses port 8099")
check("127.0.0.1:8099" in read("os/board/eitri-plus4/rootfs-overlay/etc/nginx/nginx.conf"), "nginx proxies the configurator on 8099")
check("/data/eitri/flavor" in read("klipper-layer/scripts/overlay-install.sh"), "overlay-install reads /data/eitri/flavor")
check("flavor_file" in read("configurator/eitri_configurator/settings.py"), "configurator writes the flavor file")
for unit in ("klipper", "moonraker", "klarity", "eitri-camera", "go2rtc", "eitri-configurator"):
    check(f"{unit}.service" in read("os/board/eitri-plus4/rootfs-overlay/usr/lib/systemd/system-preset/50-eitri.preset"),
          f"{unit}.service is in the OS preset")
    check(unit in read("os/board/eitri-plus4/rootfs-overlay/usr/share/eitri/factory/moonraker.asvc"), f"{unit} is in moonraker.asvc")
    check(f'"{unit}.service"' in read("os/board/eitri-plus4/rootfs-overlay/usr/share/polkit-1/rules.d/50-eitri.rules"),
          f"{unit}.service is in the polkit rule")
mcu_layer = read("klipper-layer/config/printer.cfg.tmpl")
check(re.search(r"\[mcu\]\n(?:.*\n)*?serial: @@mcu_toolhead_serial@@", mcu_layer) is not None, "layer: [mcu] is the toolhead")
check(re.search(r"\[mcu U_1\]\n(?:.*\n)*?serial: @@mcu_mainboard_serial@@", mcu_layer) is not None, "layer: [mcu U_1] is the mainboard")
check("[verify_heater chamber]" in mcu_layer, "layer: the chamber heater keeps its verify_heater")
check("serial_port: /dev/ttyS1" in read("klarity/config/klarity.cfg"), "klarity default panel UART is documented in klarity.cfg")

# 7. workflows ---------------------------------------------------------------------------------------
wf_dir = ROOT / ".github/workflows"
uses_re = re.compile(r"^\s*(?:-\s+)?uses:\s*(\S+)(?:\s+#\s*(\S+))?", re.M)
for wf in sorted(wf_dir.glob("*.yml")):
    text = wf.read_text()
    for m in uses_re.finditer(text):
        ref, ver = m.group(1), m.group(2)
        if ref.startswith("./"):
            continue
        check(bool(re.fullmatch(r"[\w.-]+/[\w./-]+@[0-9a-f]{40}", ref)), f"{wf.name}: action pinned to a commit SHA: {ref}")
        check(bool(ver and re.fullmatch(r"v\d+(\.\d+)*", ver)), f"{wf.name}: pinned action {ref.split('@')[0]} has a version comment")
check((ROOT / ".github/dependabot.yml").exists() and "github-actions" in (ROOT / ".github/dependabot.yml").read_text(),
      "dependabot keeps the pinned actions current")

osimg = (wf_dir / "os-image.yml").read_text()
steps = re.split(r"(?m)^      - ", osimg)
by_name = {m.group(1): st for st in steps if (m := re.match(r"name:\s*(.+)", st))}
# Presence checks (`secrets.X != ''`) pass only a boolean; handing a step the value itself is what is limited.
writers = [n for n, st in by_name.items() if "${{ secrets.EITRI_RAUC_KEY }}" in st]
check("Decide development or release build" in by_name, "os-image: mode step exists")
check(writers == ["Write release keys"], f"os-image: only the key-writing step reads the signing key secret (found {writers})")
order = [m.group(1).strip() for m in re.finditer(r"(?m)^      - (?:name:\s*(.+)|uses:)", osimg) if m.group(1)]
if "Write release keys" in order and "Build" in order and "Remove release keys" in order:
    w, b, r = order.index("Write release keys"), order.index("Build"), order.index("Remove release keys")
    check(w + 1 == b and b + 1 == r, "os-image: keys exist only around the Build step")
    check(r < order.index("Test the image tooling against the host tools that were just built"),
          "os-image: keys are removed before the tests run")
else:
    check(False, "os-image: Write release keys / Build / Remove release keys steps exist")
for name in ("os-image.yml", "legal.yml"):
    t = (wf_dir / name).read_text()
    m = re.search(r"(?m)^      - name: Restore (?:download and compiler|download) caches?\n(?P<body>(?:        .*\n)+)", t)
    check(bool(m) and "if: ${{ !startsWith(github.ref, 'refs/tags/') }}" in m.group("body"),
          f"{name}: release (tag) builds do not restore caches written by other runs")
pub = osimg[osimg.index("  publish:"):]
check("gh release view" in pub and "gh release upload" in pub and "--clobber" in pub,
      "os-image: publish tolerates a release that legal.yml already created")
check(not re.search(r"gh release create \"\$GITHUB_REF_NAME\" dist", pub), "os-image: publish does not fail on an existing release")

print(f"\n{passed} checks passed, {len(failures)} failed")
sys.exit(1 if failures else 0)
