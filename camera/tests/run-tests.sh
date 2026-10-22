#!/bin/sh
# run-tests.sh: offline test suite for the Eitri camera layer.
#
# Covers: script syntax, unit/udev/go2rtc/Moonraker invariants, the tuning logic
# (against a v4l2-ctl test double), probe verdicts, bench measurement against a
# paced fake ustreamer, and an end-to-end go2rtc MJPEG passthrough check.
#
# Anything that needs the real camera, the real ustreamer binary or the real
# systemd unit is NOT simulated here; see camera/README.md "Hardware checks".
#
# Usage: tests/run-tests.sh          (exit 0 = all run checks passed)

set -u

HERE=$(cd "$(dirname "$0")" && pwd)
CAM=$(cd "$HERE/.." && pwd)
FIX="$HERE/fixtures"
FAKE_BIN="$FIX/bin"

PASS=0
FAIL=0
SKIP=0
TMP=$(mktemp -d "${TMPDIR:-/tmp}/eitri-camera-tests.XXXXXX") || exit 1
PIDS=""

cleanup() {
    for p in $PIDS; do
        kill "$p" 2>/dev/null
    done
    rm -rf "$TMP"
}
trap cleanup EXIT INT TERM

ok() {
    PASS=$((PASS + 1))
    echo "  ok    $1"
}

bad() {
    FAIL=$((FAIL + 1))
    echo "  FAIL  $1${2:+ ($2)}"
}

skip() {
    SKIP=$((SKIP + 1))
    echo "  skip  $1 ($2)"
}

check_eq() {
    if [ "$2" = "$3" ]; then
        ok "$1"
    else
        bad "$1" "got '$3', wanted '$2'"
    fi
}

check_contains() {
    case "$2" in
        *"$3"*) ok "$1" ;;
        *) bad "$1" "missing '$3'" ;;
    esac
}

check_absent() {
    case "$2" in
        *"$3"*) bad "$1" "unexpected '$3'" ;;
        *) ok "$1" ;;
    esac
}

have() {
    command -v "$1" >/dev/null 2>&1
}

# A loopback port unlikely to collide with anything else on the host.
free_port() {
    echo $((20000 + $$ % 2000 + $1))
}

start_fake_ustreamer() { # port fps width height
    python3 -I "$FIX/fake_ustreamer.py" "$1" "$2" "$3" "$4" >"$TMP/fake-$1.log" 2>&1 &
    PIDS="$PIDS $!"
    i=0
    while [ "$i" -lt 50 ]; do
        if curl -s -m 1 -o /dev/null "http://127.0.0.1:$1/snapshot"; then
            return 0
        fi
        i=$((i + 1))
        sleep 0.1
    done
    return 1
}

echo "== static checks"

for f in "$CAM"/bin/eitri-camera-tune "$CAM"/bin/eitri-camera-probe-mpp \
    "$CAM"/bin/eitri-go2rtc-auth \
    "$CAM"/bench/bench.sh "$HERE/run-tests.sh" "$FAKE_BIN/v4l2-ctl"; do
    if sh -n "$f" 2>/dev/null; then
        ok "sh -n $(basename "$f")"
    else
        bad "sh -n $(basename "$f")" "syntax error"
    fi
done

for f in "$CAM"/bin/eitri-camera-tune "$CAM"/bin/eitri-camera-probe-mpp \
    "$CAM"/bin/eitri-go2rtc-auth "$CAM"/bench/bench.sh; do
    if [ -x "$f" ]; then
        ok "executable $(basename "$f")"
    else
        bad "executable $(basename "$f")"
    fi
done

if python3 -I -c "import ast,sys; ast.parse(open(sys.argv[1]).read())" "$FIX/fake_ustreamer.py" 2>/dev/null; then
    ok "python parses fake_ustreamer.py"
else
    bad "python parses fake_ustreamer.py"
fi

UNIT="$CAM/systemd/eitri-camera.service"
unit=$(cat "$UNIT")
check_contains "unit: MJPEG passthrough format" "$unit" "--format=MJPEG"
check_contains "unit: HW encoder (passthrough)" "$unit" "--encoder=HW"
check_contains "unit: 1920x1080" "$unit" "--resolution=1920x1080"
check_contains "unit: 30 fps" "$unit" "--desired-fps=30"
check_contains "unit: port 8080" "$unit" "--port=8080"
check_contains "unit: loopback host" "$unit" "--host=127.0.0.1"
check_contains "unit: eitri-cam device ordering" "$unit" "After=dev-eitri\\x2dcam.device"
check_contains "unit: skipped (not looped) when no camera is attached" "$unit" "ConditionPathExists=/dev/eitri-cam"
check_contains "unit: wait step is a short bounded check" "$unit" "Environment=EITRI_CAMERA_WAIT_SECONDS=5"
check_absent "unit: no Wants on the device (hotplug is udev's job)" "$(grep -v '^#' "$UNIT")" "Wants="
check_contains "unit: wait step is fatal" "$unit" "ExecStartPre=/usr/lib/eitri/camera/eitri-camera-tune wait"
check_contains "unit: tune step is non-fatal" "$unit" "ExecStartPre=-/usr/lib/eitri/camera/eitri-camera-tune tune"
check_contains "unit: runs as eitri" "$unit" "User=eitri"
check_absent "unit: no YUYV fallback" "$unit" "YUYV"
check_absent "unit: no CPU re-encode" "$unit" "--encoder=CPU"
check_absent "unit: no drop-same-frames (pointless for webcams)" "$unit" "--drop-same-frames"

UDEV="$CAM/udev/99-eitri-camera.rules"
if have udevadm; then
    if udevadm verify --resolve-names=never "$UDEV" >"$TMP/udev.out" 2>&1; then
        ok "udevadm verify 99-eitri-camera.rules"
    else
        bad "udevadm verify 99-eitri-camera.rules" "$(tail -n 3 "$TMP/udev.out")"
    fi
else
    skip "udevadm verify" "udevadm not installed"
fi
udev=$(cat "$UDEV")
check_contains "udev: known camera rule" "$udev" 'ATTRS{idProduct}=="0b09"'
check_contains "udev: generic fallback rule" "$udev" 'ATTRS{idVendor}=="?*"'
# Both rules must start the unit on hotplug: without it a fallback UVC camera
# plugged in after boot (the unit was skipped for lack of a node) never streams.
rules=$(grep -c '^SUBSYSTEM==.*SYMLINK+="eitri-cam".*ENV{SYSTEMD_WANTS}+="eitri-camera.service"' "$UDEV")
check_eq "udev: every eitri-cam rule starts eitri-camera.service" 2 "$rules"
check_eq "udev: no eitri-cam rule without SYSTEMD_WANTS" 0 \
    "$(grep '^SUBSYSTEM==.*SYMLINK+="eitri-cam"' "$UDEV" | grep -vc 'SYSTEMD_WANTS')"

G2UNIT=$(cat "$CAM/systemd/go2rtc.service")
check_contains "go2rtc unit: generates credentials first" "$G2UNIT" "ExecStartPre=/usr/lib/eitri/camera/eitri-go2rtc-auth ensure"
check_contains "go2rtc unit: refuses an open config" "$G2UNIT" "ExecStartPre=/usr/lib/eitri/camera/eitri-go2rtc-auth check"
check_contains "go2rtc unit: loads the credentials file" "$G2UNIT" "EnvironmentFile=-/data/printer_data/config/go2rtc-auth.env"

if python3 -I - "$CAM/go2rtc/go2rtc.yaml" "$CAM/go2rtc/profiles/software-h264.yaml.example" <<'PY' 2>"$TMP/yaml.err"
import sys
import yaml
main = yaml.safe_load(open(sys.argv[1]))
assert main["streams"]["eitri_cam"] == ["http://127.0.0.1:8080/stream"], main["streams"]
assert main["api"]["listen"].startswith("127.0.0.1:"), "API must stay on loopback"
assert main["webrtc"]["listen"] == "", "WebRTC is unauthenticated and cannot carry MJPEG: keep it off"
assert main["rtsp"]["username"] == "${EITRI_RTSP_USER}", main["rtsp"]
assert main["rtsp"]["password"] == "${EITRI_RTSP_PASS}", main["rtsp"]
prof = yaml.safe_load(open(sys.argv[2]))
assert prof["streams"]["eitri_cam_h264"] == ["ffmpeg:eitri_cam#video=h264"], prof
PY
then
    ok "go2rtc.yaml and profile parse with expected streams"
else
    bad "go2rtc.yaml and profile parse" "$(tail -n 1 "$TMP/yaml.err")"
fi

if python3 -I - "$CAM/moonraker/webcam.cfg" <<'PY' 2>"$TMP/mr.err"
import configparser
import sys
c = configparser.ConfigParser()
c.read(sys.argv[1])
s = c["webcam eitri"]
assert s["service"] == "mjpegstreamer", s["service"]
assert s["stream_url"] == "/webcam/stream", s["stream_url"]
assert s["snapshot_url"] == "/webcam/snapshot", s["snapshot_url"]
assert int(s["target_fps"]) == 30
PY
then
    ok "moonraker webcam.cfg parses with mjpegstreamer service"
else
    bad "moonraker webcam.cfg parses" "$(tail -n 1 "$TMP/mr.err")"
fi

NGINX=$(cat "$CAM/moonraker/nginx-webcam.inc")
check_contains "nginx: proxy strips /webcam/ prefix" "$NGINX" "proxy_pass http://127.0.0.1:8080/;"
check_contains "nginx: buffering off for streams" "$NGINX" "proxy_buffering off;"

# ----------------------------------------------------------------------------
echo "== tuning logic"

new_tune_case() {
    CASE=$(mktemp -d "$TMP/tune.XXXX") || exit 1
    DEV="$CASE/eitri-cam"
    : > "$DEV"
    STATE="$CASE/state"
    : > "$STATE"
    LOG="$CASE/calls.log"
    : > "$LOG"
    export EITRI_CAMERA_DEVICE="$DEV"
    export EITRI_CAMERA_V4L2CTL="$FAKE_BIN/v4l2-ctl"
    export FAKE_V4L2_CONTROLS="$FIX/controls-uvc.txt"
    export FAKE_V4L2_STATE="$STATE"
    export FAKE_V4L2_LOG="$LOG"
    export FAKE_V4L2_DIR="$FIX/v4l2"
    export FAKE_V4L2_REJECT=""
    export FAKE_V4L2_IGNORE=""
    export EITRI_CAMERA_WAIT_SECONDS=1
    unset EITRI_CAMERA_EXPOSURE_MODE EITRI_CAMERA_EXPOSURE_ABS EITRI_CAMERA_POWER_LINE
}

# run_tune SUBCOMMAND: sets RC and ERR (stderr of the script).
run_tune() {
    ERR=$("$CAM/bin/eitri-camera-tune" "$1" 2>&1 >/dev/null)
    RC=$?
}

state_of() {
    sed -n "s/^$1=//p" "$STATE" | tail -n 1
}

new_tune_case
run_tune wait
check_eq "wait: present node returns 0" 0 "$RC"

new_tune_case
rm -f "$DEV"
run_tune wait
check_eq "wait: missing node fails (so systemd retries)" 1 "$RC"
check_contains "wait: says which node" "$ERR" "$DEV"

new_tune_case
run_tune tune
check_eq "tune default: exits 0" 0 "$RC"
check_eq "tune default: auto-priority cleared" 0 "$(state_of exposure_auto_priority)"
check_contains "tune default: dynamic framerate skipped when absent" "$ERR" "skip exposure_dynamic_framerate"
check_eq "tune default: fixed exposure (Manual Mode)" 1 "$(state_of exposure_auto)"
check_eq "tune default: 33.3 ms with the camera on 60 Hz" 333 "$(state_of exposure_absolute)"

new_tune_case
EITRI_CAMERA_EXPOSURE_MODE=auto run_tune tune
check_eq "tune auto: leaves exposure_auto alone" "" "$(state_of exposure_auto)"
check_absent "tune auto: no exposure_absolute write" "$(cat "$LOG")" "exposure_absolute"

# Kernel 6.x control names (the X-Plus 4 camera on Eitri's 6.18 kernel), camera set to 50 Hz.
new_tune_case
FAKE_V4L2_CONTROLS="$FIX/controls-uvc-618.txt" EITRI_CAMERA_CTRLS="gain=4 bogus" run_tune tune
check_eq "tune 6.x names: auto_exposure=1 (Manual Mode)" 1 "$(state_of auto_exposure)"
check_eq "tune 6.x names: 30 ms with the camera on 50 Hz" 300 "$(state_of exposure_time_absolute)"
check_eq "tune extra controls: gain=4" 4 "$(state_of gain)"
check_contains "tune extra controls: malformed entry warned" "$ERR" "ignoring 'bogus'"

new_tune_case
FAKE_V4L2_CONTROLS="$FIX/controls-uvc-618.txt" EITRI_CAMERA_POWER_LINE=60 run_tune tune
check_eq "tune power line 60 then default exposure: 33.3 ms" 333 "$(state_of exposure_time_absolute)"

new_tune_case
EITRI_CAMERA_EXPOSURE_MODE=manual EITRI_CAMERA_EXPOSURE_ABS=250 run_tune tune
check_eq "tune manual: exits 0" 0 "$RC"
check_eq "tune manual: exposure_auto=1 (Manual Mode)" 1 "$(state_of exposure_auto)"
check_eq "tune manual: exposure_absolute=250" 250 "$(state_of exposure_absolute)"

new_tune_case
EITRI_CAMERA_EXPOSURE_MODE=manual EITRI_CAMERA_EXPOSURE_ABS=500 run_tune tune
check_contains "tune manual: warns when exposure exceeds 1/30 s" "$ERR" "exceeds 1/30 s"
check_eq "tune manual: still applies the requested value" 500 "$(state_of exposure_absolute)"

new_tune_case
EITRI_CAMERA_EXPOSURE_MODE=manual EITRI_CAMERA_EXPOSURE_ABS=abc run_tune tune
check_eq "tune manual bad value: exits 0" 0 "$RC"
check_contains "tune manual bad value: warns" "$ERR" "must be a positive integer"
check_eq "tune manual bad value: stays auto" "" "$(state_of exposure_auto)"

new_tune_case
EITRI_CAMERA_EXPOSURE_MODE=turbo run_tune tune
check_eq "tune bad mode: exits 0" 0 "$RC"
check_contains "tune bad mode: warns" "$ERR" "unknown EITRI_CAMERA_EXPOSURE_MODE"
check_eq "tune bad mode: no exposure change" "" "$(state_of exposure_auto)"

new_tune_case
EITRI_CAMERA_POWER_LINE=50 run_tune tune
check_eq "tune power line 50: power_line_frequency=1" 1 "$(state_of power_line_frequency)"

new_tune_case
EITRI_CAMERA_POWER_LINE=off run_tune tune
check_eq "tune power line off: power_line_frequency=0" 0 "$(state_of power_line_frequency)"

new_tune_case
EITRI_CAMERA_POWER_LINE=bogus run_tune tune
check_contains "tune power line bogus: warns, no write" "$ERR" "unknown EITRI_CAMERA_POWER_LINE"

new_tune_case
FAKE_V4L2_REJECT="exposure_auto_priority" run_tune tune
check_eq "tune rejected control: still exits 0" 0 "$RC"
check_contains "tune rejected control: warns" "$ERR" "could not set exposure_auto_priority"

new_tune_case
FAKE_V4L2_IGNORE="exposure_auto_priority" run_tune tune
check_eq "tune silently ignored control: exits 0" 0 "$RC"
check_contains "tune silently ignored control: read-back catches it" "$ERR" "reads back as"

new_tune_case
EITRI_CAMERA_V4L2CTL="$CASE/no-such-v4l2-ctl" run_tune tune
check_eq "tune without v4l2-ctl: exits 0" 0 "$RC"
check_contains "tune without v4l2-ctl: says so" "$ERR" "not installed"

new_tune_case
rm -f "$DEV"
run_tune tune
check_eq "tune without node: exits 0" 0 "$RC"
check_contains "tune without node: says so" "$ERR" "nothing to tune"

new_tune_case
"$CAM/bin/eitri-camera-tune" >/dev/null 2>&1
check_eq "usage: no subcommand exits 2" 2 "$?"

# ----------------------------------------------------------------------------
echo "== go2rtc credentials guard"

AUTH="$CAM/bin/eitri-go2rtc-auth"
GCASE=$(mktemp -d "$TMP/auth.XXXX") || exit 1
export EITRI_GO2RTC_AUTH_FILE="$GCASE/go2rtc-auth.env"
unset EITRI_GO2RTC_ALLOW_UNSAFE EITRI_RTSP_USER EITRI_RTSP_PASS

"$AUTH" >/dev/null 2>&1
check_eq "auth: no subcommand exits 2" 2 "$?"

"$AUTH" ensure 2>/dev/null
check_eq "auth ensure: exits 0" 0 "$?"
check_eq "auth ensure: file mode is 0600" 600 "$(stat -c %a "$EITRI_GO2RTC_AUTH_FILE")"
a_user=$(sed -n 's/^EITRI_RTSP_USER=//p' "$EITRI_GO2RTC_AUTH_FILE")
a_pass=$(sed -n 's/^EITRI_RTSP_PASS=//p' "$EITRI_GO2RTC_AUTH_FILE")
check_eq "auth ensure: user is eitri" eitri "$a_user"
check_eq "auth ensure: password is 36 hex characters" 36 "${#a_pass}"
case "$a_pass" in
    *[!0-9a-f]*) bad "auth ensure: password is hex" "$a_pass" ;;
    *) ok "auth ensure: password is hex" ;;
esac
before=$(cat "$EITRI_GO2RTC_AUTH_FILE")
"$AUTH" ensure 2>/dev/null
check_eq "auth ensure: existing credentials are never rewritten" "$before" "$(cat "$EITRI_GO2RTC_AUTH_FILE")"
check_eq "auth ensure: leaves no temp files behind" 1 "$(ls -A "$GCASE" | wc -l)"

rm -f "$EITRI_GO2RTC_AUTH_FILE"
"$AUTH" ensure 2>/dev/null
second=$(sed -n 's/^EITRI_RTSP_PASS=//p' "$EITRI_GO2RTC_AUTH_FILE")
if [ "$second" != "$a_pass" ] && [ -n "$second" ]; then
    ok "auth ensure: each install gets its own random password"
else
    bad "auth ensure: each install gets its own random password"
fi

printf 'EITRI_RTSP_USER=eitri\nEITRI_RTSP_PASS=\n' >"$EITRI_GO2RTC_AUTH_FILE"
"$AUTH" ensure 2>/dev/null
check_eq "auth ensure: replaces a file with an empty password" "" \
    "$(sed -n 's/^EITRI_RTSP_PASS=$//p' "$EITRI_GO2RTC_AUTH_FILE")"
check_eq "auth ensure: and the new password is not empty" 36 \
    "$(sed -n 's/^EITRI_RTSP_PASS=//p' "$EITRI_GO2RTC_AUTH_FILE" | awk '{ print length($0) }')"

EITRI_GO2RTC_AUTH_FILE="$GCASE/missing-dir/auth.env" "$AUTH" ensure >/dev/null 2>&1
check_eq "auth ensure: missing directory fails" 1 "$?"

# check: the shipped config, with the credentials the unit would load.
a_user=$(sed -n 's/^EITRI_RTSP_USER=//p' "$EITRI_GO2RTC_AUTH_FILE")
a_pass=$(sed -n 's/^EITRI_RTSP_PASS=//p' "$EITRI_GO2RTC_AUTH_FILE")
auth_check() { # config [env assignments via caller]
    ERR=$(EITRI_GO2RTC_CONFIG="$1" "$AUTH" check 2>&1 >/dev/null)
    RC=$?
}

EITRI_RTSP_USER=$a_user EITRI_RTSP_PASS=$a_pass auth_check "$CAM/go2rtc/go2rtc.yaml"
check_eq "auth check: shipped config with credentials passes" 0 "$RC"

auth_check "$CAM/go2rtc/go2rtc.yaml"
check_eq "auth check: shipped config without credentials in env fails" 1 "$RC"
check_contains "auth check: names the unset variable problem" "$ERR" "variable that is not set"

cfg() { # name; reads body from stdin
    cat > "$GCASE/$1.yaml"
    echo "$GCASE/$1.yaml"
}

c=$(cfg old-default <<'Y'
api:
  listen: "127.0.0.1:1984"
rtsp:
  listen: ":8554"
  # Add username/password here before exposing RTSP beyond a trusted LAN.
webrtc:
  listen: ":8555"
Y
)
auth_check "$c"
check_eq "auth check: the previous shipped config (open RTSP and WebRTC) fails" 1 "$RC"
check_contains "auth check: flags open RTSP" "$ERR" "rtsp.username and rtsp.password"
check_contains "auth check: flags open WebRTC" "$ERR" "webrtc.listen is ':8555'"

c=$(cfg no-sections <<'Y'
streams:
  eitri_cam:
    - http://127.0.0.1:8080/stream
Y
)
auth_check "$c"
check_eq "auth check: omitted sections (go2rtc defaults are public) fail" 1 "$RC"
check_contains "auth check: flags the default public API" "$ERR" "api.listen"
check_contains "auth check: flags the default public WebRTC" "$ERR" "webrtc.listen is not set"

c=$(cfg public-api <<'Y'
api:
  listen: ":1984"
rtsp:
  listen: "127.0.0.1:8554"
webrtc:
  listen: ""
Y
)
auth_check "$c"
check_eq "auth check: public API fails even with loopback RTSP" 1 "$RC"

c=$(cfg loopback-only <<'Y'
api:
  listen: "127.0.0.1:1984"
rtsp:
  listen: "127.0.0.1:8554"
webrtc:
  listen: "127.0.0.1:8555"
Y
)
auth_check "$c"
check_eq "auth check: everything on loopback needs no credentials" 0 "$RC"

c=$(cfg literal-creds <<'Y'
api:
  listen: "127.0.0.1:1984"
rtsp:
  listen: ":8554"
  username: viewer
  password: 'hunter2hunter2'   # a trailing comment
webrtc:
  listen: ""
Y
)
auth_check "$c"
check_eq "auth check: literal credentials in the config pass" 0 "$RC"

c=$(cfg empty-password <<'Y'
api:
  listen: "127.0.0.1:1984"
rtsp:
  listen: ":8554"
  username: viewer
  password: ""
webrtc:
  listen: ""
Y
)
auth_check "$c"
check_eq "auth check: empty password fails" 1 "$RC"

c=$(cfg default-substitution <<'Y'
api:
  listen: "127.0.0.1:1984"
rtsp:
  listen: ":8554"
  username: ${EITRI_RTSP_USER:viewer}
  password: ${EITRI_RTSP_PASS}
webrtc:
  listen: ""
Y
)
EITRI_RTSP_PASS= auth_check "$c"
check_eq "auth check: unset password variable is not accepted as a literal" 1 "$RC"
EITRI_RTSP_PASS=abc auth_check "$c"
check_eq "auth check: \${VAR:default} user with a set password passes" 0 "$RC"

auth_check "$GCASE/does-not-exist.yaml"
check_eq "auth check: unreadable config fails" 1 "$RC"

c=$(cfg old-default2 <<'Y'
api:
  listen: ":1984"
rtsp:
  listen: ":8554"
Y
)
ERR=$(EITRI_GO2RTC_ALLOW_UNSAFE=1 EITRI_GO2RTC_CONFIG="$c" "$AUTH" check 2>&1 >/dev/null)
check_eq "auth check: EITRI_GO2RTC_ALLOW_UNSAFE=1 downgrades to a warning" 0 "$?"
check_contains "auth check: and still says what is open" "$ERR" "warning"
unset EITRI_GO2RTC_AUTH_FILE

# ----------------------------------------------------------------------------
echo "== MPP / H.264 encoder probe"

probe_case() { # node names to create under a fresh nodes/ directory
    PCASE=$(mktemp -d "$TMP/probe.XXXX") || exit 1
    mkdir -p "$PCASE/nodes"
    for n in "$@"; do
        : > "$PCASE/nodes/$n"
    done
    PLOG="$PCASE/out"
}

probe_case video0 video1 video2
EITRI_CAMERA_VIDEO_GLOB="$PCASE/nodes/video*" \
    EITRI_CAMERA_V4L2CTL="$FAKE_BIN/v4l2-ctl" FAKE_V4L2_DIR="$FIX/v4l2" \
    EITRI_CAMERA_MPP_DEV="$PCASE/no-mpp" EITRI_CAMERA_KCONFIG="$PCASE/no-config" \
    "$CAM/bin/eitri-camera-probe-mpp" >"$PLOG" 2>&1
rc=$?
check_eq "probe with H.264 encoder: exit 0" 0 "$rc"
check_contains "probe with H.264 encoder: reports encoder" "$(cat "$PLOG")" "-> H.264 ENCODER"
check_contains "probe with H.264 encoder: reports decoder" "$(cat "$PLOG")" "decoder (H.264 on the compressed input side)"
check_absent "probe: camera not mistaken for M2M" "$(grep 'video0' "$PLOG")" "memory-to-memory=yes"

probe_case video0 video1
EITRI_CAMERA_VIDEO_GLOB="$PCASE/nodes/video*" \
    EITRI_CAMERA_V4L2CTL="$FAKE_BIN/v4l2-ctl" FAKE_V4L2_DIR="$FIX/v4l2" \
    EITRI_CAMERA_MPP_DEV="$PCASE/no-mpp" EITRI_CAMERA_KCONFIG="$PCASE/no-config" \
    "$CAM/bin/eitri-camera-probe-mpp" >"$PLOG" 2>&1
rc=$?
check_eq "probe decoder only: exit 1" 1 "$rc"
check_contains "probe decoder only: honest verdict" "$(cat "$PLOG")" "No usable V4L2 H.264 encoder found"
check_contains "probe decoder only: names MJPEG path" "$(cat "$PLOG")" "MJPEG passthrough"

probe_case video0
mkdir -p "$PCASE/mpp"
: > "$PCASE/mpp/service"
EITRI_CAMERA_VIDEO_GLOB="$PCASE/nodes/video*" EITRI_CAMERA_V4L2CTL="$FAKE_BIN/v4l2-ctl" \
    FAKE_V4L2_DIR="$FIX/v4l2" EITRI_CAMERA_MPP_DEV="$PCASE/mpp/service" \
    EITRI_CAMERA_KCONFIG="$PCASE/no-config" "$CAM/bin/eitri-camera-probe-mpp" >"$PLOG" 2>&1
check_contains "probe MPP node without encoder: not counted as usable" "$(cat "$PLOG")" "Eitri does not ship MPP userspace"

"$CAM/bin/eitri-camera-probe-mpp" extra >/dev/null 2>&1
check_eq "probe: unexpected argument exits 2" 2 "$?"

# ----------------------------------------------------------------------------
echo "== bench"

BENCH="$CAM/bench/bench.sh"

"$BENCH" --mode bogus >/dev/null 2>&1
check_eq "bench: bad mode exits 2" 2 "$?"

"$BENCH" --mode stream --seconds 0 >/dev/null 2>&1
check_eq "bench: zero window exits 2" 2 "$?"

PORT_UNREACHABLE=$(free_port 7)
out=$("$BENCH" --mode stream --url "http://127.0.0.1:$PORT_UNREACHABLE/stream" --seconds 2 2>&1)
check_eq "bench stream: unreachable URL exits 1" 1 "$?"
check_contains "bench stream: unreachable URL says no frames" "$out" "no frames captured"

BCASE=$(mktemp -d "$TMP/bench.XXXX") || exit 1
: > "$BCASE/eitri-cam"
out=$(FAKE_V4L2_CONTROLS="$FIX/controls-uvc.txt" FAKE_V4L2_FRAME_BYTES=1000 \
    PATH="$FAKE_BIN:$PATH" "$BENCH" --mode raw --device "$BCASE/eitri-cam" --seconds 2 --fps 30 2>&1)
rc=$?
check_eq "bench raw (stub): exits 0" 0 "$rc"
check_eq "bench raw (stub): reports 60 frames in 2 s" 60 "$(echo "$out" | awk '/^raw/ { print $2 }')"

# A systemctl stand-in that reports eitri-camera.service as active.
mkdir -p "$BCASE/busy"
printf '#!/bin/sh\n# Pretend eitri-camera.service is running.\nexit 0\n' > "$BCASE/busy/systemctl"
chmod +x "$BCASE/busy/systemctl"
out=$(PATH="$BCASE/busy:$FAKE_BIN:$PATH" "$BENCH" --mode raw --device "$BCASE/eitri-cam" --seconds 1 2>&1)
check_eq "bench raw: refuses while service holds the camera (exit 3)" 3 "$?"
check_contains "bench raw: says to stop the service" "$out" "systemctl stop eitri-camera"

if python3 -I -c "import PIL" 2>/dev/null && have curl; then
    FPS_PORT=$(free_port 11)
    if start_fake_ustreamer "$FPS_PORT" 30 1920 1080; then
        out=$("$BENCH" --mode stream --url "http://127.0.0.1:$FPS_PORT/stream" --seconds 5 2>&1)
        rc=$?
        check_eq "bench stream (fake 30 fps): exits 0" 0 "$rc"
        fps=$(echo "$out" | awk '/^stream/ { print $4 }')
        if awk -v f="$fps" 'BEGIN { exit !(f >= 27 && f <= 33) }'; then
            ok "bench stream (fake 30 fps): measured fps $fps within 27..33"
        else
            bad "bench stream (fake 30 fps): measured fps" "got '$fps'"
        fi
    else
        skip "bench stream fake server" "could not start fake ustreamer"
    fi
else
    skip "bench stream fake server" "PIL or curl missing"
fi

# ----------------------------------------------------------------------------
echo "== go2rtc end to end"

if have go2rtc && have ffprobe && python3 -I -c "import PIL" 2>/dev/null; then
    G_PORT=$(free_port 13)
    if start_fake_ustreamer "$G_PORT" 30 640 360; then
        # Shipped config, with ports moved onto this test's range.
        GCFG="$TMP/go2rtc.yaml"
        sed -e "s#127.0.0.1:1984#127.0.0.1:$((G_PORT + 1))#" \
            -e "s#:8554#:$((G_PORT + 2))#" \
            -e "s#http://127.0.0.1:8080/stream#http://127.0.0.1:$G_PORT/stream#" \
            "$CAM/go2rtc/go2rtc.yaml" > "$GCFG"
        EITRI_RTSP_USER=eitri EITRI_RTSP_PASS=test-only-secret \
            timeout 30 go2rtc -config "$GCFG" >"$TMP/go2rtc.log" 2>&1 &
        PIDS="$PIDS $!"
        sleep 2
        codec=$(timeout 15 ffprobe -v error -rtsp_transport tcp -show_entries stream=codec_name \
            -of default=nw=1:nk=1 "rtsp://eitri:test-only-secret@127.0.0.1:$((G_PORT + 2))/eitri_cam" 2>/dev/null | head -n 1)
        check_eq "go2rtc passthrough: RTSP codec is mjpeg (no transcode)" "mjpeg" "$codec"
        # go2rtc skips RTSP authentication for loopback clients, so enforcement can
        # only be observed from a non-loopback address of this host.
        LAN_IP=$(ip -4 -o addr show scope global 2>/dev/null | awk '{ sub("/.*", "", $4); print $4; exit }')
        if [ -n "$LAN_IP" ]; then
            RTSP_LAN="$LAN_IP:$((G_PORT + 2))"
            codec=$(timeout 15 ffprobe -v error -rtsp_transport tcp -show_entries stream=codec_name \
                -of default=nw=1:nk=1 "rtsp://eitri:test-only-secret@$RTSP_LAN/eitri_cam" 2>/dev/null | head -n 1)
            check_eq "go2rtc auth: non-loopback client with credentials gets mjpeg" "mjpeg" "$codec"
            codec=$(timeout 15 ffprobe -v error -rtsp_transport tcp -show_entries stream=codec_name \
                -of default=nw=1:nk=1 "rtsp://$RTSP_LAN/eitri_cam" 2>/dev/null | head -n 1)
            check_eq "go2rtc auth: non-loopback client without credentials is refused" "" "$codec"
            codec=$(timeout 15 ffprobe -v error -rtsp_transport tcp -show_entries stream=codec_name \
                -of default=nw=1:nk=1 "rtsp://eitri:wrong@$RTSP_LAN/eitri_cam" 2>/dev/null | head -n 1)
            check_eq "go2rtc auth: non-loopback client with a wrong password is refused" "" "$codec"
        else
            skip "go2rtc auth enforcement" "no non-loopback IPv4 address (go2rtc skips auth for loopback)"
        fi
        if ss -ltn 2>/dev/null | grep -q ':8555 '; then
            skip "go2rtc passthrough: no WebRTC listener" "something else holds :8555 on this host"
        elif grep -q '\[webrtc\] listen' "$TMP/go2rtc.log"; then
            bad "go2rtc passthrough: no WebRTC listener" "$(grep '\[webrtc\]' "$TMP/go2rtc.log" | head -n 1)"
        else
            ok "go2rtc passthrough: starts no WebRTC listener"
        fi

        # Optional software-H.264 profile, merged into a config with its own ports.
        PCFG="$TMP/go2rtc-h264.yaml"
        cat > "$PCFG" <<PROFILE
api:
  listen: "127.0.0.1:$((G_PORT + 4))"
rtsp:
  listen: "127.0.0.1:$((G_PORT + 5))"
webrtc:
  listen: "127.0.0.1:$((G_PORT + 6))"
streams:
  eitri_cam:
    - http://127.0.0.1:$G_PORT/stream
  eitri_cam_h264:
    - ffmpeg:eitri_cam#video=h264
PROFILE
        timeout 40 go2rtc -config "$PCFG" >"$TMP/go2rtc-h264.log" 2>&1 &
        PIDS="$PIDS $!"
        sleep 2
        codec=$(timeout 25 ffprobe -v error -rtsp_transport tcp -show_entries stream=codec_name \
            -of default=nw=1:nk=1 "rtsp://127.0.0.1:$((G_PORT + 5))/eitri_cam_h264" 2>/dev/null | head -n 1)
        check_eq "go2rtc optional profile: RTSP codec is h264" "h264" "$codec"
    else
        skip "go2rtc end to end" "could not start fake ustreamer"
    fi
else
    skip "go2rtc end to end" "go2rtc, ffprobe or PIL missing"
fi

# ----------------------------------------------------------------------------
echo "== hardware-only checks (not run here)"
skip "real Sunplus 1bcf:0b09 node, formats and fps" "needs the camera on the printer"
skip "ustreamer binary and eitri-camera.service start" "needs the Eitri image"
skip "udev symlink /dev/eitri-cam on a real plug" "needs the camera and udevd"
skip "MPP / hardware H.264 encoder presence" "needs the RK3328 board"

echo
echo "passed: $PASS  failed: $FAIL  skipped: $SKIP"
[ "$FAIL" -eq 0 ]
