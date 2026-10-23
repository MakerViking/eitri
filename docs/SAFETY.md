# Safety

A 3D printer runs a 300 °C+ hotend, a 100 °C+ bed and a heated chamber, unattended, off mains power. Read this before you
print with Eitri.

> **Pre-alpha.** Eitri has printed on one QIDI X-Plus 4. Many of its safety mechanisms have unit and real-klippy tests but
> have **not** been exercised on a machine yet; they are listed in [What is not yet verified](#what-is-not-yet-verified).
> Do not leave an Eitri printer printing unattended.

## How to stop the printer

| Method | What it does | Status |
|---|---|---|
| **Mains power switch** | Cuts everything | Always works. Use it if anything looks wrong |
| **Emergency stop in the web interface** (Mainsail/Fluidd, top bar) | Klipper `M112`: heaters and motors off at once, then the firmware needs a restart | Standard Klipper/Moonraker path; not yet exercised on an Eitri printer |
| **Stop** on the touchscreen's printing page | Asks for confirmation, then **cancels** the print (a normal cancel, not an emergency stop) | Works |
| **E-stop icon on the touchscreen's home page** | **Nothing yet.** It is deliberately disabled until the button's identity on the stock screen is confirmed, so a misidentified button can never halt a print | **Does not work.** Use one of the methods above |

## Rules while Eitri is pre-alpha

- **Stay with the printer while it prints.**
- **Power-cycle instead of rebooting.** A warm `reboot` hangs this board (the stock boot firmware does not reset the SoC).
  After an update, or whenever the system asks for a reboot, switch the printer off at the mains, wait 5 seconds, and
  switch it on again. A fix is in the image but has not been verified on hardware yet.
- **Calibrate the probe before the first print.** The probe's Z offset ships as a 0 placeholder, and `PRINT_START` refuses to
  run until `PROBE_CALIBRATE` + `SAVE_CONFIG` has set a real one. Do not bypass that check unless 0 really is correct.
- **Never weaken a heater check to get a print going.** If Klipper shuts down with a heater error, find the cause.

## What protects you today

These are active in the shipped configuration (`klipper-layer/config/printer.cfg.tmpl`):

- **Heater runaway protection** (`[verify_heater]`) on the hotend, the bed and the chamber. The hotend and bed use Klipper's
  default checks. The chamber uses QIDI's slower limits, because its small air heater cannot heat fast enough for the
  defaults, but it is still checked.
- **Temperature limits:** hotend 380 °C, bed 125 °C, chamber 70 °C. Above the limit Klipper shuts down.
- **Chamber backstop:** a second chamber sensor shuts the printer down above 85 °C (the stock firmware allowed 140 °C). If it
  trips during normal heating, raise `chamber_backstop_max_temp` (keep it above 70 °C); never remove the sensor.
- **Chamber/bed power interlock** (`qd_power_interlock`): the chamber heater shares the bed's power budget and only gets
  power while the bed is at its target. It can only remove heater power, and if it cannot install itself Klipper refuses to
  start.
- **Probe safety:** a probe that is already triggered before a move stops homing with an error. If you see
  "Probe triggered prior to movement", stop and find out why before retrying.
- **Motors stay energised during a long pause**, so the toolhead cannot drop or drift before you resume.
- **Shell commands from G-code are restricted:** no G-code source (the API, a macro, an uploaded print file) can add
  arguments to a configured shell command unless that command explicitly allows them (`qd_shell_guard`).

## What is not yet verified

On a real machine, none of these has been exercised yet. Treat them as untested until they have been:

- The emergency stop from the web interface (see the table above).
- Heater runaway detection actually firing on this printer's heaters.
- The 85 °C chamber backstop against where the sensor physically sits.
- The bed/chamber interlock against the real power supply.
- Power-loss recovery (resume after a power cut).
- The probe's polarity and Z offset on your unit (calibrate before printing).
- Input shaping and the bed mesh on your unit.

## Reporting a safety problem

If Eitri does something unsafe (a heater that keeps heating after a stop, a check that does not fire, a motor that moves
when it should not), stop using it and report it. See [`SECURITY.md`](../SECURITY.md) for how to report privately.
