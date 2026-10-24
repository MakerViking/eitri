# Crash reports and diagnostics

Eitri can collect what is needed to find a crash or a misbehaving printer, and can send it to its
developers. **Nothing leaves the printer unless you allow it**, and what would leave is written to
the printer first, so you can read it.

## Two kinds of report

| | Diagnostics report | Crash report |
|---|---|---|
| Made by | the `EITRI_REPORT` macro, when you ask | the printer itself, at the boot after a crash (or `EITRI_REPORT_SEND`) |
| Where it goes | the **Logs** folder, as `eitri-report-<time>.tar.gz` | the **Logs** folder, as `eitri-crash-report-<time>.json` |
| Sent anywhere | never; you attach it to a bug report yourself | only if your setting allows it (below) |
| Contents | system log of this and the previous boot, kernel crash records, Klipper and Moonraker logs, the printer configuration, a system snapshot | the end of the kernel and system log before the crash, kernel crash records, the end of Klipper's log, versions and temperatures |

Run a macro from the macro buttons in Fluidd or Mainsail, or type it in the console.

## What counts as a crash

A printer is often switched off at the mains, so a boot that did not end with a clean shutdown is not
a crash by itself. Eitri counts one when:

* the kernel left a crash record (a panic or a CPU lockup; the record survives the reset in reserved
  RAM, see `linux.fragment` and the device tree), or
* the previous boot ended without a clean shutdown **while a heater was on**, that is while the printer
  was in use: a hang, a reset or a power loss in the middle of a job.

The check runs at boot, before Klipper starts (`eitri-crash-check.service`).

## What is removed first

Before a report is written, the printer replaces everything that identifies it, you or your network
with a placeholder (`<lan-ip-1>`, `<hostname-1>`, ...; the same value always gets the same
placeholder, so the logs still read correctly):

* IP addresses (private ones become `<lan-ip-N>`, others `<ip-N>`; loopback and multicast stay) and
  MAC addresses
* the host name and the extra host names in `eitri-hosts.txt`
* Wi-Fi network names, and Wi-Fi keys should one ever appear in a log
* the board's serial number, the machine id, and the serial numbers of USB devices such as the MCUs
* e-mail addresses, credentials in URLs, tokens, API keys, passwords and the camera password
* the names of your G-code files

Not collected at all: G-code files, `moonraker.conf`, the Moonraker database, SSH keys and access
state, network connection files, camera images. Scrubbing is careful, not perfect: read a report
before you share it.

## Your setting

`eitri-reports.txt`, with the configuration files (Fluidd or Mainsail can edit it):

| Setting | What happens after a crash |
|---|---|
| `off` (the default) | Nothing is sent. The crash report stays in the Logs folder. |
| `ask` | Fluidd and Mainsail show a dialog: **Send**, **Do not send**, or **Always send**. It names the file in the Logs folder so you can read it first, and comes back when a browser connects until you answer. |
| `always` | The report is sent without asking. |

The setup wizard asks this (Options, *Crash reports*). Change it any time with
`EITRI_CRASH_REPORTS MODE=off`, `MODE=ask` or `MODE=always`, or by editing the file. A report you
asked for with `EITRI_REPORT_SEND` is always asked about, whatever the setting, and never offers
*Always send*.

## Where a report goes

To the Eitri developers, through TinkerAtlas's bug-report service (`https://tinkeratlas.com`,
the same one SindriCAD and BrokkrSculpt use), **without an account**: the printer sends no token,
no user name and no e-mail address. The request body is exactly the JSON file in the Logs folder. A
sent report is renamed to `eitri-crash-report-<time>.sent.json`; one that TinkerAtlas refused stays
as it was. TinkerAtlas sees the connection's IP address, as any web service does.

## How it is built

The part that reads the journal runs as root **without network access**
(`eitri-report`, `eitri-report.service` and `eitri-crash-check.service` have `PrivateNetwork=yes`).
The only part that can send runs as the unprivileged service user (`eitri-report-sender`, started
when a report is pending) and can read the scrubbed report and your setting, never the journal. The
root side reads and writes in the service user's folders without following symbolic links, so a
link planted there cannot make it read or overwrite another file. Report generation runs at idle
priority so it cannot disturb a print.

Source: `os/board/eitri-plus4/rootfs-overlay/usr/libexec/eitri/eitri-report` and
`eitri-report-sender`; tests in `os/tests/test_report.py` and `os/tests/test_crash_reports.py`.
