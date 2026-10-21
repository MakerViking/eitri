# Security policy

## Status

Eitri is **pre-alpha**. No release has been published, so no version is supported for security fixes yet. Once the first release exists, this file will list the supported versions and their end-of-support dates.

## Reporting a vulnerability

Please **do not** open a public issue for a security problem.

Report it privately through GitHub's private vulnerability reporting for this repository (the **Security** tab, then **Report a vulnerability**). Include:

- the component and the file or service involved, with the commit or release identifier;
- the steps to reproduce, and the impact you expect;
- whether the issue needs physical access, a LAN position, or internet exposure;
- whether you have already disclosed it elsewhere.

If you cannot use GitHub's reporting feature, open a public issue that says only that you have a security report and asks for a private channel. Do not include details in that issue.

## What we aim to do

These are goals for a small volunteer project, not contractual service levels.

- Acknowledge a report within 7 days.
- Confirm or rule out the issue, and say which, within 14 days.
- Agree a disclosure timeline with the reporter. The default is 90 days from the report, shorter for issues that are already being exploited.
- Credit the reporter in the advisory, unless they ask not to be named.

## Scope

In scope:

- the Klarity screen daemon and the configurator, including their network interfaces;
- the `qd_*` Klipper extras, where a flaw could cause unsafe heater or motion behavior;
- the release tooling and the update path: signing (`tools/release/`, `.github/workflows/release.yml`), bundle and seed verification, key handling, and rollback;
- the recovery and MCU-flashing tooling.

Out of scope:

- vulnerabilities in upstream Klipper, Kalico, Moonraker, nginx, ustreamer, or the kernel. Report those to their projects. We will track and ship the fixes through the pins in `release.lock`.
- Exposure caused by port-forwarding a printer to the internet, which the documentation advises against.

## Trust model

Eitri assumes a printer on a home LAN that its owner controls, with a physical owner who can open the case. This section describes what the image is built to do; **none of it has run on a printer yet** ([`STATUS.md`](STATUS.md) lists what is verified and what is not). It is **not** designed to be exposed to the internet or to defend against someone who already has a shell on the printer. What it does promise:

**Who may do what on the network**

* The printer's web interface, API, webcam page and slicer uploads are all on **port 80** through nginx. nginx accepts connections from loopback, private (RFC 1918) and link-local **IPv4** addresses only. There is **no IPv6 listener** (the allow-list is IPv4-only, so listening on IPv6 would have bypassed it).
* **Moonraker listens on `127.0.0.1` only.** Slicers and apps use port 80; nothing reaches Moonraker's own port from the LAN, and there are no wildcard CORS or trusted-client lists.
* **Web interface sign-in is a choice made in setup.** The default is *Trust my home network*: anyone on an allowed address can use the printer, like the stock firmware. *Require sign-in* puts a login in front of the web interface for networks that are shared. Neither option makes the printer safe to expose to the internet.
* The **camera RTSP stream is password protected** (a random password per install; WebRTC is off, and go2rtc refuses to start with a non-loopback listener and no credentials). The password is shown on its own page after setup, not in the wizard. `/webcam/` on port 80 is part of the web interface and is protected like it (the LAN allow-list, plus the sign-in if you chose it).

**Setup and shell access**

* The setup wizard (port 8099) needs a one-time pairing code that the printer shows on its own touchscreen, closes when setup is finished, **and exits** (the service stops). Only the **Unlock setup** action on the touchscreen, or `sudo -n eitri-configurator unlock` in a shell, starts it again with a fresh code. It refuses changes while printing.
* **Shell access is off by default.** A new install has no password login. The optional remote-access step of the wizard adds an SSH public key and/or sets a password for the `eitri` user through a narrow privileged helper (`eitri-access`); it can be undone with the same step. sshd allows password login only if a password was set.
* The `eitri` account may run only `eitri-update`, `eitri-bootctl`, `eitri-configurator unlock` and `eitri-access` as root, without a password. The Klipper and Moonraker services run unprivileged with `NoNewPrivileges`.

**What Klipper's shell extension may do**

* `gcode_shell_command` **denies extra `PARAMS` by default**: a macro or a G-code line cannot append arguments to a command that the configuration defined. An owner may opt in per command in the configuration.

**Where the code and the updates come from**

* The OS root file systems are read-only and updated as A/B slots through RAUC bundles that are signed; an unsigned or wrongly signed bundle is refused. `/data` is writable by the services and is **not** a trust root: no key directory is read from it.
* Release metadata is signed with minisign (Ed25519) and checked against the keys that ship in the RAUC-verified image; manifests carry an expiry and a sequence number that never goes backwards, and a revocation list ships with the image. Nothing is installed on the strength of a digest that is not inside a signed document.
* The data seed (Klipper or Kalico, Moonraker, the Python environments, Klarity, the extras) is built by CI from hash-pinned inputs, announced by a signed document, verified by SHA-256 before it is read, and unpacked with a strict member policy. An offline first boot therefore executes only code that is in the release.
* **Release signing keys are never in a build job.** CI builds unsigned artifacts; a separate job in a protected GitHub environment, approved by the maintainer, signs them. The root keys are held offline. How this works, and what it does not protect against, is in [`docs/RELEASING.md`](docs/RELEASING.md).

**What is not defended**

* Someone on your LAN with a browser, when you chose *Trust my home network*.
* Someone with physical access: the boot partition, the serial console and the maskrom button are all reachable inside the case.
* A compromised GitHub account that holds both the ability to start the release workflow and to approve its environment, or a compromised runner during the signing job ([`docs/RELEASING.md`](docs/RELEASING.md#5-deferred-signing)).
* Upstream bugs in Klipper, Kalico, Moonraker, nginx, ustreamer or the kernel, until the pin moves.

## Deployment guidance

The Moonraker API, nginx, the configurator, the camera streams, and go2rtc are **LAN services**. Do not forward any of their ports from your router to the internet. Use a VPN if you need remote access.

## Safety-related reports

A bug that could let a heater, motor, or probe behave unsafely is a safety issue as well as a security issue. Report it through the same channel. Until it is fixed, make sure the printer cannot run unattended.
