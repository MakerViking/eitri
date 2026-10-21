#!/bin/sh
# bench.sh: measure what the Eitri camera actually delivers.
#
# Two measurements over the same time window:
#   stream  HTTP MJPEG from ustreamer: what Mainsail, Fluidd, OBS and go2rtc receive.
#   raw     V4L2 capture straight from the device: what the camera itself produces.
# If raw fps is low, the camera (or its exposure settings) is the limit. If raw is
# fine but stream is low, the streamer or the network is the bottleneck.
#
# The raw test needs exclusive access to the device, so stop eitri-camera.service
# first. The stream test works while the service runs.
#
# Frame counts: stream frames are counted from ustreamer's per-part Content-Type
# header. Raw frames are the requested count, reported only if v4l2-ctl exits 0.
#
# Usage: bench.sh [--mode stream|raw|both] [--url URL] [--device DEV]
#                 [--seconds N] [--fps N] [--width W] [--height H]
# Exit: 0 ok, 1 measurement failed, 2 bad usage, 3 raw skipped (device busy).

set -u

URL="http://127.0.0.1:8080/stream"
DEVICE="/dev/eitri-cam"
WINDOW=20
MODE="both"
TARGET_FPS=30
WIDTH=1920
HEIGHT=1080

usage() {
    cat <<'USAGE'
Usage: bench.sh [options]
  --mode stream|raw|both   which measurement(s) to run (default both)
  --url URL                MJPEG stream URL (default http://127.0.0.1:8080/stream)
  --device DEV             V4L2 node for the raw test (default /dev/eitri-cam)
  --seconds N              measurement window per test (default 20)
  --fps N                  target fps; sets the raw frame count (default 30)
  --width W --height H     raw test resolution (default 1920x1080)
USAGE
}

die() {
    echo "bench.sh: $*" >&2
    exit 2
}

while [ "$#" -gt 0 ]; do
    case "$1" in
        --mode) MODE="${2:-}"; shift 2 ;;
        --url) URL="${2:-}"; shift 2 ;;
        --device) DEVICE="${2:-}"; shift 2 ;;
        --seconds) WINDOW="${2:-}"; shift 2 ;;
        --fps) TARGET_FPS="${2:-}"; shift 2 ;;
        --width) WIDTH="${2:-}"; shift 2 ;;
        --height) HEIGHT="${2:-}"; shift 2 ;;
        -h | --help) usage; exit 0 ;;
        *) usage >&2; die "unknown option: $1" ;;
    esac
done

case "$WINDOW" in '' | *[!0-9]*) die "--seconds must be a positive integer" ;; esac
case "$TARGET_FPS" in '' | *[!0-9]*) die "--fps must be a positive integer" ;; esac
[ "$WINDOW" -gt 0 ] || die "--seconds must be > 0"
[ "$TARGET_FPS" -gt 0 ] || die "--fps must be > 0"
case "$MODE" in
    stream | raw | both) ;;
    *) die "--mode must be stream, raw or both" ;;
esac

# Monotonic-ish clock with centisecond resolution where /proc is available.
now() {
    if [ -r /proc/uptime ]; then
        read -r up _ < /proc/uptime
        echo "$up"
    else
        date +%s
    fi
}

report() {
    # name frames bytes t0 t1
    awk -v name="$1" -v frames="$2" -v bytes="$3" -v t0="$4" -v t1="$5" 'BEGIN {
        secs = t1 - t0
        if (secs <= 0 || frames <= 0 || bytes <= 0) {
            printf "%-7s no frames captured\n", name
            exit 1
        }
        printf "%-7s %7d %9.2f %8.2f %12.1f %9.2f %10.1f\n",
            name, frames, secs, frames / secs, bytes / frames / 1024,
            bytes / secs / 1000000, bytes * 8 / secs / 1000000
    }'
}

header() {
    printf '%-7s %7s %9s %8s %12s %9s %10s\n' \
        mode frames seconds fps avg_frame_KiB MB/s Mbit/s
}

bench_stream() {
    tmpdir=$(mktemp -d) || die "mktemp failed"
    mkfifo "$tmpdir/bytes" || die "mkfifo failed"
    wc -c < "$tmpdir/bytes" > "$tmpdir/total" &
    t0=$(now)
    # curl --max-time bounds the window; its exit status 28 is expected here.
    frames=$(curl -sN --max-time "$WINDOW" "$URL" 2>/dev/null |
        tee "$tmpdir/bytes" |
        grep -a -o -- 'Content-Type: image/jpeg' |
        wc -l | tr -d ' ')
    t1=$(now)
    wait
    bytes=$(tr -d ' ' < "$tmpdir/total")
    rm -rf "$tmpdir"
    report stream "${frames:-0}" "${bytes:-0}" "$t0" "$t1"
}

bench_raw() {
    if command -v systemctl >/dev/null 2>&1 &&
        systemctl is-active --quiet eitri-camera.service 2>/dev/null; then
        echo "bench.sh: eitri-camera.service holds the camera; run 'systemctl stop eitri-camera' for the raw test" >&2
        return 3
    fi
    if [ ! -e "$DEVICE" ]; then
        echo "bench.sh: no device at $DEVICE" >&2
        return 1
    fi
    count=$((WINDOW * TARGET_FPS))
    tmpdir=$(mktemp -d) || die "mktemp failed"
    mkfifo "$tmpdir/bytes" || die "mkfifo failed"
    wc -c < "$tmpdir/bytes" > "$tmpdir/total" &
    # Hold the FIFO's write end here, so the reader cannot see EOF until we close it
    # below, even if v4l2-ctl writes nothing. Without this a silent capture hangs.
    exec 3> "$tmpdir/bytes"
    t0=$(now)
    v4l2-ctl -d "$DEVICE" \
        --set-fmt-video="width=$WIDTH,height=$HEIGHT,pixelformat=MJPG" \
        --set-parm="$TARGET_FPS" --stream-mmap=4 --stream-count="$count" \
        --stream-to="$tmpdir/bytes" >/dev/null 2>"$tmpdir/err"
    capture_rc=$?
    t1=$(now)
    exec 3>&-
    wait
    if [ "$capture_rc" -ne 0 ]; then
        echo "bench.sh: raw capture failed (v4l2-ctl exit $capture_rc):" >&2
        cat "$tmpdir/err" >&2
        rm -rf "$tmpdir"
        return 1
    fi
    bytes=$(tr -d ' ' < "$tmpdir/total")
    rm -rf "$tmpdir"
    report raw "$count" "${bytes:-0}" "$t0" "$t1"
}

rc=0
header
case "$MODE" in
    stream)
        bench_stream || rc=1
        ;;
    raw)
        bench_raw || rc=$?
        ;;
    both)
        bench_stream || rc=1
        bench_raw || rc=$?
        ;;
esac
exit "$rc"
