#!/usr/bin/env python3
"""Test double for ustreamer's HTTP interface (/stream and /snapshot).

Serves a paced multipart MJPEG stream the same way ustreamer does: boundary
"boundarydonotcross", one JPEG part per frame. Used only by the test suite and
the bench self-test; never installed on the printer.

Usage: fake_ustreamer.py PORT FPS WIDTH HEIGHT [FRAMES]
  FRAMES bounds the stream length (default: unbounded).
"""

import io
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from PIL import Image, ImageDraw

BOUNDARY = "boundarydonotcross"


def make_jpeg(width, height, index):
    img = Image.new("RGB", (width, height), (index * 7 % 256, 80, 160))
    draw = ImageDraw.Draw(img)
    draw.rectangle([0, 0, width // 2, height // 2], fill=(255, 255, 255))
    draw.text((20, 20), f"frame {index}", fill=(0, 0, 0))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=80)
    return buf.getvalue()


def main():
    port = int(sys.argv[1])
    fps = float(sys.argv[2])
    width = int(sys.argv[3])
    height = int(sys.argv[4])
    max_frames = int(sys.argv[5]) if len(sys.argv) > 5 else None
    period = 1.0 / fps
    snapshot = make_jpeg(width, height, 0)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            pass

        def do_GET(self):
            if self.path.startswith("/snapshot"):
                self.send_response(200)
                self.send_header("Content-Type", "image/jpeg")
                self.send_header("Content-Length", str(len(snapshot)))
                self.end_headers()
                self.wfile.write(snapshot)
                return
            if not self.path.startswith("/stream"):
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Age", "0")
            self.send_header("Cache-Control", "no-cache, private")
            self.send_header("Pragma", "no-cache")
            self.send_header(
                "Content-Type", f"multipart/x-mixed-replace; boundary={BOUNDARY}"
            )
            self.end_headers()
            index = 0
            next_due = time.monotonic()
            try:
                while max_frames is None or index < max_frames:
                    frame = make_jpeg(width, height, index)
                    self.wfile.write(
                        f"--{BOUNDARY}\r\nContent-Type: image/jpeg\r\n"
                        f"Content-Length: {len(frame)}\r\n"
                        f"X-Timestamp: {time.time():.6f}\r\n\r\n".encode()
                    )
                    self.wfile.write(frame)
                    self.wfile.write(b"\r\n")
                    self.wfile.flush()
                    index += 1
                    next_due += period
                    delay = next_due - time.monotonic()
                    if delay > 0:
                        time.sleep(delay)
            except (BrokenPipeError, ConnectionResetError):
                pass

    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()


if __name__ == "__main__":
    main()
