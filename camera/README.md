# Eitri camera layer

The camera path for the QIDI X-Plus 4 (Firefly ROC-RK3328-CC): one USB UVC camera
(Sunplus `1bcf:0b09`) served at full **1920x1080 MJPEG, 30 fps**, with no
transcoding on the board.

```
/dev/video4 (UVC, single opener)
   └─ ustreamer  (eitri-camera.service, MJPEG passthrough, 127.0.0.1:8080)
        ├─ nginx /webcam/  → Fluidd / Mainsail   (Moonraker [webcam eitri])
        ├─ go2rtc RTSP     → OBS (rtsp://<printer>:8554/eitri_cam)   [go2rtc.service]
        └─ /snapshot       → Klarity touchscreen preview (polls, never opens the camera)
```

Design rules:

- **Passthrough is the default.** `--format=MJPEG --encoder=HW` forwards the camera's own
  JPEG frames. Neither MJPEG is decoded nor re-encoded, so the four A53 cores stay free for Klipper.
- **One opener.** Only ustreamer opens the camera. Everything else reads HTTP.
- **Resolution is fixed at 1920x1080.** The unit does not let `camera.env` change format or resolution.
- **The hardware H.264 encoder is not assumed.** `eitri-camera-probe-mpp` reports what the board actually exposes.

## Layout

| Path | Purpose | Installed to (suggested) |
|---|---|---|
| `bin/eitri-camera-tune` | `wait` for the node; `tune` V4L2 controls (never fatal) | `/usr/lib/eitri/camera/` |
| `bin/eitri-go2rtc-auth` | `ensure` RTSP credentials exist; `check` that go2rtc's config is not open to the LAN | `/usr/lib/eitri/camera/` |
| `bin/eitri-camera-probe-mpp` | Honest report of V4L2 H.264 encoders and MPP | `/usr/lib/eitri/camera/` |
| `bench/bench.sh` | FPS and bandwidth: `/stream` vs raw V4L2 | `/usr/lib/eitri/camera/` |
| `systemd/eitri-camera.service` | ustreamer unit | `/usr/lib/systemd/system/` |
| `systemd/go2rtc.service` | optional RTSP gateway for OBS | `/usr/lib/systemd/system/` |
| `udev/99-eitri-camera.rules` | `/dev/eitri-cam` symlink, group `video` | `/usr/lib/udev/rules.d/` |
| `go2rtc/go2rtc.yaml` | passthrough config (default) | `/usr/share/eitri/camera/`, copied to `/data/printer_data/config/` on first install |
| `go2rtc/profiles/software-h264.yaml.example` | optional software H.264 profile (not loaded) | docs only |
| `moonraker/webcam.cfg` | `[webcam eitri]` section | appended to `moonraker.conf` |
| `moonraker/nginx-webcam.inc` | `/webcam/` proxy location | included in the Fluidd/Mainsail server block |
| `config/camera.env.example` | site tuning overrides | copied to `/data/printer_data/config/camera.env` on request |
| `tests/run-tests.sh` | offline test suite (no camera needed) | not installed |

Runtime dependencies on the image (pins belong in `release.lock`):

- `ustreamer` v6.67 at `/usr/bin/ustreamer` (`[camera.ustreamer]`). Buildroot's package is older, so it needs an override.
- `go2rtc` v1.9.14 at `/usr/bin/go2rtc` (`[camera.go2rtc]`). Not in Buildroot; ship the linux_arm64 binary.
- `v4l-utils` (`v4l2-ctl`) for the tune script, the probe and the raw bench.
- `curl` for the stream bench. `ffmpeg` only if the optional H.264 profile is enabled.

## Install

```sh
install -Dm0755 bin/eitri-camera-tune            /usr/lib/eitri/camera/eitri-camera-tune
install -Dm0755 bin/eitri-camera-probe-mpp       /usr/lib/eitri/camera/eitri-camera-probe-mpp
install -Dm0755 bin/eitri-go2rtc-auth            /usr/lib/eitri/camera/eitri-go2rtc-auth
install -Dm0755 bench/bench.sh                   /usr/lib/eitri/camera/bench.sh
install -Dm0644 systemd/eitri-camera.service     /usr/lib/systemd/system/eitri-camera.service
install -Dm0644 systemd/go2rtc.service           /usr/lib/systemd/system/go2rtc.service
install -Dm0644 udev/99-eitri-camera.rules       /usr/lib/udev/rules.d/99-eitri-camera.rules
install -Dm0644 go2rtc/go2rtc.yaml               /usr/share/eitri/camera/go2rtc.yaml
udevadm control --reload && udevadm trigger --subsystem-match=video4linux
systemctl daemon-reload && systemctl enable --now eitri-camera
```

Notes on the unit: `ConditionPathExists=/dev/eitri-cam` skips the unit when no camera is attached, so
boot never waits on an absent camera and nothing loops. Both udev rules start the unit on hotplug
(`SYSTEMD_WANTS`), which is what brings it up when a camera appears later. After that the
`ExecStartPre` wait (5 s) only bridges the moment between the udev event and the symlink; it fails the start if the
node is missing, and `Restart=on-failure` retries after 3 s. The `tune` step is prefixed with `-`, so a
v4l2 problem can never block the stream. If you set `EITRI_CAMERA_DEVICE` to another node in `camera.env`, clear the
condition with a drop-in (`[Unit]` then `ConditionPathExists=`).

## Frame-rate tuning (why the camera might fall to ~24 fps)

Before this layer, the camera delivered a flat ~23.7 fps at both 1080p and 720p. That flatness points
at **auto-exposure lowering the frame rate** in dim light, not at the bus or the CPU. The tune step
clears `exposure_auto_priority` and `exposure_dynamic_framerate` (whichever the kernel exposes), so
the sensor holds its rate and auto-exposure adjusts within it.

This is a hypothesis until measured. Confirm it on the printer:

```sh
# Before tuning (stop the service first; the raw test needs the camera):
systemctl stop eitri-camera
/usr/lib/eitri/camera/bench.sh --mode raw --seconds 20
# After tuning, with the service running again:
systemctl start eitri-camera
/usr/lib/eitri/camera/bench.sh --mode both --seconds 20
```

If raw is still ~24 fps after tuning, the camera's own MJPEG mode is the limit at 1080p. Then 30 fps
is not achievable with this sensor, and the honest fix is a lower frame rate, not a different
streamer. Use `v4l2-ctl -d /dev/eitri-cam --list-formats-ext` to see which intervals the camera offers.

Optional exposure tuning goes in `/data/printer_data/config/camera.env` (see `config/camera.env.example`).
Keep manual exposure at or below 333 (1/30 s in 100 µs units), or the frame rate drops.

## Bench

```sh
bench/bench.sh --mode stream --seconds 20   # HTTP /stream; works while the service runs
bench/bench.sh --mode raw    --seconds 20   # raw V4L2; requires the service to be stopped
bench/bench.sh --mode both                  # both, in turn
```

Output columns: frames, seconds, fps, average frame size, MB/s and Mbit/s. Stream frames are
counted from ustreamer's per-part headers. Raw frames are the requested count, reported only
when v4l2-ctl exits 0.

## Probe for an H.264 encoder (MPP)

```sh
/usr/lib/eitri/camera/eitri-camera-probe-mpp
```

It lists every V4L2 memory-to-memory node with its compressed and raw queue formats, checks for
`/dev/mpp_service` and `librockchip_mpp`, and reads the relevant kernel config. Exit 0 means a V4L2 H.264
encoder node was found. Exit 1 means none was found, which is the expected result on the RK3328
mainline image today. An MPP device without a V4L2 encoder is reported as not usable, because Eitri does not ship MPP userspace.

## OBS setup

The camera is MJPEG, so OBS needs one of two ways to read it:

**A. Browser Source (simplest, works everywhere).**
1. Add a *Browser* source. URL: `http://<printer-ip>/webcam/stream`.
2. Width `1920`, height `1080`. Leave *Shutdown source when not visible* off.
3. If the stream stops after a scene change, enable *Refresh browser when scene becomes active*.

**B. Media Source over RTSP (lower overhead, needs go2rtc.service).**
1. Enable `go2rtc.service` on the printer. The first start creates the RTSP credentials in
   `/data/eitri/camera/go2rtc-auth.env` (user `eitri`, random password, readable by root and `eitri` only; not in the
   configuration folder, which Moonraker serves to the network, and moved out of it on older installs). This serves
   `rtsp://eitri:<password>@<printer-ip>:8554/eitri_cam`.
2. Add a *Media Source*. Input: that URL.
3. Turn off *Local file*, enable *Restart playback when source becomes active*.
4. Disable hardware decoding if it causes trouble. MJPEG decodes fine in software on the OBS PC.
5. Turn off any network or input buffering option your OBS version offers. Lower buffering trades smoothness for latency, so check both.

Verified here: go2rtc 1.9.14 serves the stream over RTSP with codec `mjpeg` (ffprobe). **OBS itself
has not been tested against it on this project yet.** Try B, and fall back to A if OBS rejects it.

Latency tips:

- Keep the camera on the USB 2.0 path it has now, and avoid sharing its hub with other busy devices.
  Keep the printer on wired Ethernet (100 Mbit is enough for 1080p MJPEG, but not with much headroom).
- Browser Source runs in an embedded browser, which adds its own buffering. Media Source is the lower-latency choice. Measure both before picking.
- Keep the scene at 1920x1080 @ 30 fps to match the source. Scaling in OBS costs GPU time on the OBS PC, not the printer.
- Don't use the software H.264 profile for a live OBS feed on the RK3328. It is for clients that cannot read MJPEG.

## WebRTC

WebRTC does not carry MJPEG, and go2rtc's WebRTC has no authentication, so the shipped config switches it off
(`webrtc: listen: ""`; go2rtc would otherwise listen on `:8555` on every interface). A WebRTC view needs the
transcoded profile in `go2rtc/profiles/`, which is software x264 on the A53 cores and is unlikely to keep up at
1080p30. Benchmark it before relying on it. `eitri-go2rtc-auth check` refuses to start go2rtc with WebRTC on a
non-loopback address; keep it on loopback, or set `EITRI_GO2RTC_ALLOW_UNSAFE=1` in `go2rtc-auth.env` to accept the exposure knowingly.

## Security

- `rtsp://…:8554` (go2rtc) needs a password. `go2rtc.service` runs `eitri-go2rtc-auth ensure` (generates the
  credentials once) and `eitri-go2rtc-auth check` (refuses to start if the config leaves RTSP open, the API off loopback,
  or WebRTC on). The config reads `${EITRI_RTSP_USER}` and `${EITRI_RTSP_PASS}` from `go2rtc-auth.env`.
  go2rtc skips authentication for clients on the printer itself (loopback); it applies to every LAN client.
  go2rtc is disabled by default.
- `/webcam/` (nginx) is **unauthenticated**, like the rest of the printer UI on port 80: a login on this one path would
  protect nothing while the UI itself is open. Keep the printer on a trusted LAN until the UI has authentication.
- ustreamer and the go2rtc API listen on loopback only.

## Hardware checks

These need the real printer or board. The offline suite does not cover them. Record the results in release notes before shipping.

1. The camera's MJPG intervals at 1920x1080 (`v4l2-ctl -d /dev/eitri-cam --list-formats-ext`). Is 30 fps offered?
2. Which of `exposure_auto_priority` / `exposure_dynamic_framerate` the kernel exposes, and whether setting them
   to 0 lifts raw fps from ~24 to 30 in dim light (`bench.sh --mode raw` before and after).
3. `ustreamer --features` on the real build, and that `--encoder=HW` really passes frames through. Check that the stream's average
   frame size matches the raw capture's (`bench.sh --mode both`), since a re-encode would change the bytes.
4. CPU load during 1080p30 passthrough (`top` during `bench.sh --mode stream`), with Klipper and Moonraker running.
5. The udev rule on a real plug: `udevadm test` shows the `eitri-cam` symlink, and the unit starts via `SYSTEMD_WANTS`.
   Boot once with the camera unplugged (the unit must be skipped, with no ~20 s delay and no restart loop), then plug it in.
6. The unit's `ProtectSystem=strict` and `NoNewPrivileges` do not block the camera on the real image.
7. OBS: Browser Source and Media Source (RTSP, MJPEG) both render, with measured latency.
8. `eitri-camera-probe-mpp` on the board. Expected verdict: no V4L2 H.264 encoder. Keep the output with the release.
9. Real fps and Mbit/s figures for the stream and raw paths. The fake-server test confirms the bench only.
