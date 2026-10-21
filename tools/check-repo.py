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
* the shared contract: ports, service names, paths and the flavor file agree across components;
* the release trust model: the workflow structure that keeps signing keys out of build jobs (secrets only in the
  protected `sign` job's `Sign` step, the unsigned set pinned by digest between collect and sign), the `[release]`
  numbers in release.lock against the client constants, and the docs that describe them;
* licences: SPDX tags are known identifiers, copyleft tags appear only where the licence table allows, the extras
  are GPL-3.0, the QIDI-derived config is AGPL-3.0-or-later;
* the documentation covers the decisions it records (INSTALL, ARCHITECTURE, SECURITY, RELEASING).
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
             "x.egg-info/PKG-INFO", ".pytest_cache/x", ".ruff_cache/x", "eitri-release.sec", "signed/SHA256SUMS", "unsigned/INPUTS.sha256"]:
    check(ignored(path), f".gitignore ignores {path}")
for path in ["release.lock", "README.md", "LICENSE", "NOTICE", "docs/ARCHITECTURE.md", ".editorconfig", "Makefile", "STATUS.md",
             "configurator/screenshots/desktop/01-welcome.png", "klarity/src/klarity/maps/default.yaml",
             "installer/keys/eitri-release.pub", "installer/keys/eitri-root.pub", "installer/keys/revoked-keys.txt",
             "os/board/eitri-plus4/rauc/keyring.crt", "docs/RELEASING.md", "tools/release/sign-release.sh"]:
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

for name in ("os-image.yml", "legal.yml"):
    t = (wf_dir / name).read_text()
    m = re.search(r"(?m)^      - name: Restore (?:download and compiler|download) caches?\n(?P<body>(?:        .*\n)+)", t)
    check(bool(m) and "if: ${{ !startsWith(github.ref, 'refs/tags/') }}" in m.group("body"),
          f"{name}: release (tag) builds do not restore caches written by other runs")


# Workflow structure helpers: a tiny text parser, because this script runs with `python -I` and no YAML library.
def jobs_of(text: str) -> dict[str, str]:
    body = text.split("\njobs:\n", 1)[1]
    parts = re.split(r"(?m)^  ([A-Za-z0-9_-]+):[ \t]*\n", body)
    return {parts[i]: parts[i + 1] for i in range(1, len(parts) - 1, 2)}


def steps_of(job: str) -> tuple[str, dict[str, str]]:
    chunks = re.split(r"(?m)^      - ", job)
    named: dict[str, str] = {}
    for st in chunks[1:]:
        m = re.match(r"name:\s*(.+)", st)
        named[m.group(1).strip() if m else f"<uses {len(named)}>"] = st
    return chunks[0], named


SECRET_USE = re.compile(r"\$\{\{\s*secrets\.(?!GITHUB_TOKEN\b)([A-Za-z0-9_]+)")
workflows = {wf.name: wf.read_text() for wf in sorted(wf_dir.glob("*.yml"))}

# 8. release trust: deferred signing -------------------------------------------------------------------
for name, text in workflows.items():
    check("pull_request_target" not in text, f"{name}: no pull_request_target (a pull request must never reach secrets)")
    if name != "release.yml":
        check(SECRET_USE.search(text) is None, f"{name}: no secrets (only the release workflow's sign step holds any)")
        check("environment:" not in text, f"{name}: not tied to an environment (only the sign job is)")
        check(not re.search(r"-----BEGIN|\.key\b|\.sec\b|release\.crt|minisign -S", text), f"{name}: writes no key material")

rel = workflows["release.yml"]
on_block = re.search(r"(?m)^on:\n((?:  .*\n|\n)+)", rel)
check(bool(on_block) and re.findall(r"(?m)^  (\w+):", on_block.group(1)) == ["workflow_dispatch"],
      "release: starts only by hand (workflow_dispatch), never from a push, a pull request or a schedule")
check(re.search(r"(?m)^permissions:\n  contents: read\n", rel) is not None, "release: read-only token by default")
rjobs = jobs_of(rel)
check(set(rjobs) == {"collect", "sign", "publish"}, f"release: jobs are collect, sign, publish (found {sorted(rjobs)})")
if set(rjobs) == {"collect", "sign", "publish"}:
    rheads = {j: steps_of(body) for j, body in rjobs.items()}
    check("environment: release\n" in rheads["sign"][0], "release: the sign job runs in the protected 'release' environment")
    for j in ("collect", "publish"):
        check("environment:" not in rheads[j][0], f"release: the {j} job has no environment (and so no secrets)")
    check(re.search(r"(?m)^    needs: collect$", rheads["sign"][0]) is not None, "release: sign needs collect")
    check(re.search(r"(?m)^    needs: \[collect, sign\]$", rheads["publish"][0]) is not None, "release: publish needs collect and sign")
    check("contents: write" not in rheads["sign"][0] and "contents: write" not in rheads["collect"][0],
          "release: only the publish job may write to the repository")
    check("contents: write" in rheads["publish"][0], "release: publish can attach files to the draft release")
    sign_steps = rheads["sign"][1]
    users = sorted({n for n, st in sign_steps.items() if SECRET_USE.search(st)})
    check(users == ["Sign"], f"release: only the Sign step reads secrets (found {users})")
    check(not any(SECRET_USE.search(st) for n, st in rheads["collect"][1].items()) and
          not any(SECRET_USE.search(st) for n, st in rheads["publish"][1].items()), "release: collect and publish read no secrets")
    sign_step = sign_steps.get("Sign", "")
    check(sorted(set(SECRET_USE.findall(sign_step))) == ["EITRI_MINISIGN_KEY", "EITRI_MINISIGN_PASSWORD", "EITRI_RAUC_CERT", "EITRI_RAUC_KEY"],
          "release: the Sign step reads exactly the four signing secrets")
    check("trap 'rm -rf \"$keys\"' EXIT" in sign_step and "umask 077" in sign_step and "tools/release/sign-release.sh" in sign_step,
          "release: the Sign step keeps keys private, removes them on exit and calls the signing script")
    check(sum(1 for st in rjobs.values() if 'umask 077' in st) == 1, "release: keys are written in one place only")
    order = list(sign_steps)
    for before in ("Check the inputs are the ones that were reviewed", "Build the pinned RAUC"):
        check(before in order and order.index(before) < order.index("Sign"), f"release: '{before}' runs before the keys exist")
    check(all("persist-credentials: false" in st for st in (s2 for body in rjobs.values() for s2 in steps_of(body)[1].values()) if "actions/checkout" in st),
          "release: checkouts do not keep the token in the working copy")
    check("actions/cache" not in rel, "release: no caches (nothing restored from another run reaches the signing job)")
    check("release-unsigned" in rel and "INPUTS.sha256" in rel and "inputs_sha256" in rel,
          "release: the unsigned set is pinned by digest between collect and sign")
    check("--event push --commit \"$GITHUB_SHA\"" in rheads["collect"][1].get("Download the build artifacts", ""),
          "release: build artifacts are taken only from the runs of the tag's own commit")

osimg = workflows["os-image.yml"]
check("publish:" not in osimg and "gh release" not in osimg, "os-image: publishes nothing; the release workflow attaches the signed files")
mode = steps_of(jobs_of(osimg)["image"])[1].get("Decide development or release build", "")
check("rauc/keyring.crt" in mode and "installer/keys" in mode, "os-image: a tag build refuses to run without the committed public trust roots")
check("release.lock" in mode and "min_manifest_sequence" in mode, "os-image: the image floor comes from release.lock")
build = steps_of(jobs_of(osimg)["image"])[1].get("Build", "")
check("EITRI_DEFERRED_SIGNING" in build and "EITRI_MIN_MANIFEST_SEQUENCE" in build and "EITRI_REQUIRE_UPDATE_KEY" in build,
      "os-image: release builds are built for deferred signing")
check("-unsigned" in osimg, "os-image: the release artifact is named as unsigned")

# release.lock [release] and the client constants it must agree with
rel_mod_spec = __import__("importlib.util", fromlist=["util"])
spec = rel_mod_spec.spec_from_file_location("eitri_release_tool", ROOT / "tools/release/release.py")
tool = rel_mod_spec.module_from_spec(spec)
sys.modules[spec.name] = tool
spec.loader.exec_module(tool)
try:
    cfg = tool.release_settings(lock)
    check(True, "release.lock [release] is valid")
except tool.ReleaseError as exc:
    cfg = {}
    check(False, f"release.lock [release]: {exc}")
m = re.search(r'(?m)^CLOCK_FLOOR = "([^"]+)"', read("installer/eitri-update/eitri_update/manifest.py"))
check(bool(m) and cfg.get("clock_floor") == m.group(1), "release.lock clock_floor equals CLOCK_FLOOR in eitri_update/manifest.py")
check("console=ttyS" not in lock.get("release", {}).get("install", {}).get("kernel_cmdline", ""), "install.json would not put a console on an MCU UART")
check(lock.get("attributed", {}).get("qidi-plus4", {}).get("license") == "AGPL-3.0-or-later", "release.lock treats QIDI_PLUS4 as AGPL-3.0-or-later")

# the docs describe the workflow that exists
releasing = read("docs/RELEASING.md")
for secret_name in sorted(set(SECRET_USE.findall(rel))):
    check(secret_name in releasing, f"docs/RELEASING.md names the secret {secret_name}")
for word in ("CLOCK_FLOOR", "EITRI_MIN_MANIFEST_SEQUENCE", "revoked-keys.txt", "rotation", "Deferred signing", "release.yml", "sign-release.sh", "environment"):
    check(word.lower() in releasing.lower(), f"docs/RELEASING.md covers {word}")

# 9. licences and SPDX tags ------------------------------------------------------------------------
SPDX_RE = re.compile(r"(?m)^\s*(?:#|//|;|<!--|/\*)\s*SPDX-License-Identifier:\s*(.*?)\s*(?:-->|\*/)?\s*$")
CODE_SUFFIX = {".py", ".sh", ".cfg", ".tmpl", ".toml", ".service", ".js", ".dts", ".yml", ".conf", ".c", ".h", ".cmd"}
KNOWN_SPDX = {"Apache-2.0", "MIT", "GPL-3.0-or-later", "GPL-3.0-only", "AGPL-3.0-or-later", "GPL-2.0-or-later", "GPL-2.0-only",
              "GPL-2.0+ OR MIT", "GPL-2.0 WITH Linux-syscall-note", "BSD-3-Clause", "BSD-2-Clause"}
COPYLEFT_HOMES = ("klipper-layer/extras/", "klipper-layer/config/", "os/")
EXTRAS_OK = {"GPL-3.0-or-later", "GPL-3.0-only"}   # -only: a file derived from GPLv3-only code says so in its header
CONFIG_OK = {"AGPL-3.0-or-later", "Apache-2.0"}
tags: dict[str, str] = {}
for f in FILES:
    if f.suffix.lower() in CODE_SUFFIX or f.name in ("Makefile",):
        try:
            head = "\n".join(f.read_text(errors="replace").splitlines()[:200])   # long licence headers exist
        except OSError:
            continue
        m = SPDX_RE.search(head)
        if m:
            tags[str(f.relative_to(ROOT))] = m.group(1)
for rel_path, tag in sorted(tags.items()):
    check(tag in KNOWN_SPDX, f"{rel_path}: SPDX identifier {tag!r} is one the project knows (add it to tools/check-repo.py when it is deliberate)")
    if re.search(r"GPL|LGPL", tag) and "Linux-syscall" not in tag and "OR MIT" not in tag:
        check(rel_path.startswith(COPYLEFT_HOMES), f"{rel_path}: copyleft tag {tag} outside the directories the licence table allows")
extras = sorted(p for p in (ROOT / "klipper-layer/extras").glob("*.py"))
check(bool(extras), "klipper-layer/extras has modules")
for p in extras:
    check(tags.get(str(p.relative_to(ROOT))) in EXTRAS_OK, f"{p.relative_to(ROOT)}: extras are GPL-3.0-or-later (or GPL-3.0-only when derived from GPLv3-only code)")
config_docs = {"README.md", "LICENSE", "LICENSE-NOTE.md"}
cfg_files = sorted(p for p in (ROOT / "klipper-layer/config").rglob("*") if p.is_file() and p.name not in config_docs)
check(bool(cfg_files), "klipper-layer/config has files")
for p in cfg_files:
    r = str(p.relative_to(ROOT))
    check(tags.get(r) in CONFIG_OK, f"{r}: carries SPDX AGPL-3.0-or-later (adapted from QIDI) or Apache-2.0 (written for Eitri)")
    if re.search(r"QIDITECH|QIDI_PLUS4", p.read_text(errors="replace")):
        check(tags.get(r) == "AGPL-3.0-or-later", f"{r}: names QIDITECH/QIDI_PLUS4 as a source, so it must be AGPL-3.0-or-later")
for r in ("klipper-layer/config/printer.cfg.tmpl", "klipper-layer/config/macros.cfg"):
    check(tags.get(r) == "AGPL-3.0-or-later", f"{r} is AGPL-3.0-or-later")
check("GNU AFFERO GENERAL PUBLIC LICENSE" in read("klipper-layer/config/LICENSE"), "klipper-layer/config/LICENSE is the AGPL text")
for doc in ("README.md", "NOTICE", "docs/ARCHITECTURE.md"):
    text = read(doc)
    check("AGPL-3.0-or-later" in text and "klipper-layer/config" in text, f"{doc} states the AGPL licence of klipper-layer/config")
    check("GPL-3.0-or-later" in text and "klipper-layer/extras" in text, f"{doc} states the GPL licence of klipper-layer/extras")
for f in sorted((ROOT / "tools").rglob("*")):
    if f.is_file() and f.suffix in (".py", ".sh"):
        check(tags.get(str(f.relative_to(ROOT))) == "Apache-2.0", f"{f.relative_to(ROOT)} carries SPDX Apache-2.0")

# 10. the documentation matches the decisions it records ----------------------------------------------
install, arch, sec = read("docs/INSTALL.md"), read("docs/ARCHITECTURE.md"), read("SECURITY.md")
for needle in ("in-place", "data seed", "offline", "Unlock setup", "Trust my home network", "Require sign-in", "Remote access", "port 80"):
    check(needle.lower() in install.lower(), f"docs/INSTALL.md covers {needle}")
for needle in ("/data/eitri/extras", "data seed", "IPv4 only", "retire-when-locked", "eitri-access", "pairing.json", "loopback only", "DOS in-place"):
    check(needle.lower() in arch.lower(), f"docs/ARCHITECTURE.md covers {needle}")
for needle in ("Trust model", "IPv6", "127.0.0.1", "Unlock setup", "PARAMS", "Trust my home network", "deferred", "eitri-access"):
    check(needle.lower() in sec.lower(), f"SECURITY.md covers {needle}")
nginx = read("os/board/eitri-plus4/rootfs-overlay/etc/nginx/nginx.conf")
check(not re.search(r"(?m)^\s*listen\s+\[::\]", nginx), "nginx has no IPv6 listener (the allow-list is IPv4 only)")

print(f"\n{passed} checks passed, {len(failures)} failed")
sys.exit(1 if failures else 0)
