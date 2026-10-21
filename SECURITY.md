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
- the release tooling and the update path: signing, bundle verification, and rollback;
- the recovery and MCU-flashing tooling.

Out of scope:

- vulnerabilities in upstream Klipper, Kalico, Moonraker, nginx, ustreamer, or the kernel. Report those to their projects. We will track and ship the fixes through the pins in `release.lock`.
- Exposure caused by port-forwarding a printer to the internet, which the documentation advises against.

## Deployment guidance

The Moonraker API, nginx, the configurator, the camera streams, and go2rtc are **LAN services**. Do not forward any of their ports from your router to the internet. Use a VPN if you need remote access.

What the image does about it (LAN trust model):

- Moonraker listens on `127.0.0.1` only. nginx on port 80 is the one way in and accepts loopback, private (RFC 1918) and link-local IPv4 addresses. IPv6 is not filtered yet.
- The setup wizard (port 8099) needs a pairing code shown by the printer, closes itself when setup is finished, and re-opens only with `eitri-configurator unlock` on the printer.
- The camera RTSP stream is password protected (a random password is created on first start). `/webcam/` on port 80 has no login, like the rest of the web interface.
- The `eitri` account may run only `eitri-update` and `eitri-bootctl` without a password. SSH accepts keys only.

## Safety-related reports

A bug that could let a heater, motor, or probe behave unsafely is a safety issue as well as a security issue. Report it through the same channel. Until it is fixed, make sure the printer cannot run unattended.
