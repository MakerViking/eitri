# Contributing to Eitri

Thank you for helping. Eitri is **pre-alpha**. Interfaces, layouts, and pins are still moving, and most of the work is best done with someone close to the hardware. Read this whole file before you open a pull request.

## Before you start

- Read [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md). It defines the layers, the clean-tree rule, and the safety invariants that every change must keep.
- Read [`docs/CLEAN-ROOM.md`](docs/CLEAN-ROOM.md). It is a hard requirement, not a suggestion.
- For anything that touches a printer, read [`docs/RECOVERY.md`](docs/RECOVERY.md) first.

Open an issue to discuss any non-trivial change before you write it.

## Ground rules

### 1. The clean-tree rule

Klipper and Kalico are never patched. Eitri's behavior for the QIDI X-Plus 4 is added from outside:

- New Python modules go in `klipper-layer/extras/` with a **new, unique `qd_` name**.
- Config and macro files go in the KAMP-style config directory, which is symlinked into `printer_data/config`.
- Never shadow a file that upstream tracks in `klippy/extras/`.
- Never edit any `klippy/*.py` file.

A pull request that modifies upstream source trees will be closed.

### 2. Safety invariants

These must hold in every build. A change that weakens any of them will not be merged, whatever its other merits.

- `verify_heater` stays enabled. Runaway protection is never disabled, relaxed, or made conditional on a Z height or a timer.
- A probe that is already triggered when homing starts stays an **error**. It is never downgraded to an informational message.
- Retries are bounded. No probe or homing routine may retry endlessly. When retries are exhausted, the failure is raised as an error.
- The chamber and bed thermal interlock must be active before the chamber heater can be enabled.
- Heater and motion limits in shipped configs are never raised to make a test pass.

### 3. Clean-room boundary

- Do not include QIDI binaries, `.tft` or `.HMI` display assets, Cython `.so` modules, firmware dumps, or decompiler output, in any form, in any commit, issue, or attachment.
- You may reference and adapt QIDI's **public GPL sources and configs**, with attribution in `NOTICE` and under the license of the source. Check the license of the specific repository first: `QIDITECH/QIDI_PLUS4` is treated as AGPL-3.0, so anything adapted from it into `klipper-layer/config/` is AGPL-3.0-or-later. See `release.lock` under `[attributed.*]` and the Licensing section of `README.md`.
- Describe hardware behavior in terms of what you observed on hardware you own, not in terms of code you read in a closed binary.

### 4. No secrets and no personal data

Do not commit API keys, Moonraker tokens, Wi-Fi passwords, serial numbers, MAC addresses, or the contents of a live printer configuration that holds any of these. `.gitignore` blocks the common cases, but the check is your responsibility. Release signing keys are never in the repository or in a build job; `docs/RELEASING.md` explains where they live.

## Development setup

You need Python 3.14, `pytest`, and `git`. Lint with `ruff` in a throwaway virtual environment:

```sh
python3 -m venv .venv && . .venv/bin/activate
pip install ruff pytest
ruff check . && ruff format --check .
pytest
```

Shell scripts must pass `bash -n`. Run Python code from untrusted sources (for example, downloaded upstream trees) with `python3 -I`, and keep downloads under `build/<name>/`, never inside the repository.

## Making a change

1. Fork the repository and create a topic branch from `main`.
2. Keep each commit to one logical change. Write the subject in the imperative mood, at most 72 characters.
3. Explain *why* in the commit body when the reason is not obvious from the diff.
4. Add or update tests for behavior you change. For behavior that needs real hardware, describe in the pull request exactly what you could not test, and why.
5. Update `release.lock` only with the release tooling or with a verified re-check. Include the date of the check in the `verified` field.
6. Keep documentation in step with behavior. Do not document a feature as working until it works.

### Commit sign-off

Every commit must carry a `Signed-off-by:` line, which certifies the [Developer Certificate of Origin](https://developercertificate.org/). Use `git commit -s`.

### Licensing of contributions

Unless you say otherwise, your contribution is licensed under **Apache-2.0** when it lands in the repository root and its subdirectories, under **GPL-3.0-or-later** when it lands in `klipper-layer/extras/`, and under **AGPL-3.0-or-later** when it adapts QIDI's configuration into `klipper-layer/config/` (a file there that you wrote from scratch is Apache-2.0; see `klipper-layer/config/LICENSE-NOTE.md`). Put the matching `SPDX-License-Identifier` line at the top of every new file in those two directories; `tools/check-repo.py` fails without it. You confirm that you have the right to contribute it under those terms and that it contains no proprietary or clean-room-excluded material.

## Reporting bugs

Attach a diagnostics report. Run the **`EITRI_REPORT`** macro (the macro button in Fluidd or Mainsail, or type it in the console); about a minute later `eitri-report-<time>.tar.gz` appears in the **Logs** folder, ready to download. It holds the system log of this and the previous boot, the kernel's crash records, the Klipper and Moonraker logs and the printer configuration, with IP and MAC addresses, host and Wi-Fi names, serial numbers, keys, passwords and G-code file names already replaced by placeholders. Nothing is sent anywhere; look through it before you share it. Over SSH, the administrator can run `sudo /usr/libexec/eitri/eitri-report --list` to see what it would contain, or `--stdout > report.tar.gz`. After a crash, if you allowed crash reports, the printer can also send a report itself ([`docs/CRASH-REPORTS.md`](docs/CRASH-REPORTS.md)); `EITRI_REPORT_SEND` sends one on demand, after asking.

Without a report, include the Eitri version or commit, the firmware flavor (Klipper or Kalico), the relevant `systemctl status` output, and the log excerpt, and remove serial numbers, IP addresses, and tokens before you paste anything.

Security issues do not belong in the public tracker. Follow [`SECURITY.md`](SECURITY.md).

## Community standards

Participation is governed by [`CODE_OF_CONDUCT.md`](CODE_OF_CONDUCT.md).
