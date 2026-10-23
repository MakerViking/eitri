# SPDX-License-Identifier: Apache-2.0
"""The shipped nginx.conf, run for real: which Host names and which browser requests reach Moonraker.

nginx is the host's own (on PATH), or the image's aarch64 build under qemu-user when a Buildroot target tree
is around (EITRI_NGINX_TARGET, default ../build/br-out/target). Without either the tests skip; the QEMU smoke
test (os/qemu/smoke.py) checks the same rules on the booted image.
"""
from __future__ import annotations

import http.client
import http.server
import os
import re
import shutil
import signal
import socket
import subprocess
import threading
import time
from pathlib import Path

import pytest
from conftest import OVERLAY, REPO_ROOT

NGINX_DIR = OVERLAY / "etc/nginx"
TARGET = Path(os.environ.get("EITRI_NGINX_TARGET", REPO_ROOT.parent / "build/br-out/target"))


def nginx_command() -> list[str] | None:
    if shutil.which("nginx"):
        return [shutil.which("nginx")]
    if shutil.which("qemu-aarch64") and (TARGET / "usr/sbin/nginx").is_file():
        return ["qemu-aarch64", "-L", str(TARGET), str(TARGET / "usr/sbin/nginx")]
    return None


def mime_types() -> Path | None:
    for candidate in (TARGET / "etc/nginx/mime.types", Path("/etc/nginx/mime.types")):
        if candidate.is_file():
            return candidate
    return None


NGINX = nginx_command()
pytestmark = pytest.mark.skipif(NGINX is None or mime_types() is None,
                                reason="no nginx on PATH and no Buildroot target with qemu-aarch64")


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Upstream(http.server.BaseHTTPRequestHandler):
    """Stands in for Moonraker and the camera: answers every request with its method and path."""

    def _answer(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length:
            self.rfile.read(length)
        body = f"upstream {self.command} {self.path}".encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    do_GET = do_POST = _answer

    def log_message(self, *args):
        pass


@pytest.fixture(scope="module")
def server(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("nginx")
    upstream = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
    threading.Thread(target=upstream.serve_forever, daemon=True).start()
    up = upstream.server_address[1]
    port = free_port()

    ui = tmp / "ui"
    ui.mkdir()
    (ui / "index.html").write_text("the web interface")
    inc = tmp / "inc"
    inc.mkdir()
    webcam = (REPO_ROOT / "camera/moonraker/nginx-webcam.inc").read_text().replace("127.0.0.1:8080", f"127.0.0.1:{up}")
    (inc / "webcam.inc").write_text(webcam)
    hosts = tmp / "eitri-hosts.map"
    shutil.copy(NGINX_DIR / "eitri-hosts.map", hosts)

    conf = (NGINX_DIR / "nginx.conf").read_text()
    conf = re.sub(r"^user .*;$", "", conf, flags=re.M)
    conf = conf.replace("pid /run/nginx.pid;", f"pid {tmp}/nginx.pid;")
    conf = conf.replace("include /etc/nginx/mime.types;", f"include {mime_types()};")
    conf = conf.replace("include /etc/nginx/eitri-lan-only.conf;", f"include {NGINX_DIR}/eitri-lan-only.conf;")
    conf = conf.replace("include /etc/nginx/eitri-hosts.map;", f"include {hosts};")
    conf = conf.replace("include /etc/nginx/eitri/*.inc;", f"include {inc}/*.inc;")
    conf = conf.replace("server 127.0.0.1:7125;", f"server 127.0.0.1:{up};")
    conf = conf.replace("listen 0.0.0.0:80 default_server;", f"listen 127.0.0.1:{port} default_server;")
    conf = conf.replace("root /data/eitri/ui/current;", f"root {ui};")
    for d in ("client_body_temp", "proxy_temp", "fastcgi_temp", "uwsgi_temp", "scgi_temp"):
        conf = conf.replace("http {", f"http {{\n    {d}_path {tmp}/{d};", 1)
    path = tmp / "nginx.conf"
    path.write_text(conf)

    test = subprocess.run([*NGINX, "-t", "-p", str(tmp), "-c", str(path), "-e", "stderr"], capture_output=True, text=True)
    assert test.returncode == 0, test.stderr
    proc = subprocess.Popen([*NGINX, "-p", str(tmp), "-c", str(path), "-e", "stderr", "-g", "daemon off;"],
                            stderr=subprocess.PIPE, text=True)
    for _ in range(100):
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
            break
        except OSError:
            time.sleep(0.1)
    else:
        proc.kill()
        pytest.fail("nginx did not come up: " + (proc.stderr.read() if proc.stderr else ""))

    def reload():
        proc.send_signal(signal.SIGHUP)        # what `nginx -s reload` sends; direct, so it also works under qemu-user
        time.sleep(1.5)

    yield {"port": port, "hosts": hosts, "reload": reload}
    proc.terminate()
    proc.wait(timeout=10)
    upstream.shutdown()


def request(server, method, path, host, headers=None, body=None):
    conn = http.client.HTTPConnection("127.0.0.1", server["port"], timeout=20)
    conn.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
    conn.putheader("Host", host)
    for k, v in (headers or {}).items():
        conn.putheader(k, v)
    data = body.encode() if body else b""
    if body is not None:
        conn.putheader("Content-Length", str(len(data)))
    conn.endheaders(data or None)
    resp = conn.getresponse()
    text = resp.read().decode()
    conn.close()
    return resp.status, text, resp


@pytest.mark.parametrize("host", ["192.168.0.5", "eitri.local", "eitri", "printer", "localhost", "my-printer.local",
                                  "192.168.0.5:80", "EITRI.LOCAL"])
def test_names_the_printer_is_reached_by_are_served(server, host):
    status, text, _ = request(server, "GET", "/server/info", host)
    assert status == 200 and text.startswith("upstream GET /server/info"), (host, status, text)


@pytest.mark.parametrize("host", ["evil.com", "printer.home.arpa", "192.168.0.5.nip.io", "rebind.attacker.example",
                                  "-bad.local"])
def test_any_other_host_name_is_refused_everywhere(server, host):
    for path in ("/server/info", "/", "/webcam/stream"):
        status, text, resp = request(server, "GET", path, host)
        assert status == 403, (host, path, status, text)
        assert "does not answer to the name" in text and "upstream" not in text
        assert resp.getheader("Content-Type", "").startswith("text/plain")
        assert resp.getheader("X-Content-Type-Options") == "nosniff"


def test_an_extra_name_from_eitri_hosts_is_served_after_a_reload(server):
    assert request(server, "GET", "/server/info", "printer.home.arpa")[0] == 403
    original = server["hosts"].read_text()
    try:
        longest = ".".join(["a" * 63] * 3 + ["b" * 61])                       # 253 characters, the DNS maximum
        server["hosts"].write_text(original + '"printer.home.arpa" 1;\n' + f'"{longest}" 1;\n')
        server["reload"]()
        deadline = time.monotonic() + 15        # a reload starts new workers; slow under qemu-user
        while request(server, "GET", "/server/info", "printer.home.arpa")[0] != 200 and time.monotonic() < deadline:
            time.sleep(0.5)
        assert request(server, "GET", "/server/info", "printer.home.arpa")[0] == 200
        assert request(server, "GET", "/server/info", longest)[0] == 200
    finally:
        server["hosts"].write_text(original)
        server["reload"]()


@pytest.mark.parametrize("path", ["/printer/gcode/script?script=G28", "/server/files/upload", "/api/files/local",
                                  "/machine/reboot", "/access/oneshot_token", "/websocket"])
@pytest.mark.parametrize("headers", [
    {"Sec-Fetch-Site": "cross-site"},
    {"Sec-Fetch-Site": "same-site", "Origin": "http://other.local"},
    {"Origin": "http://evil.com"},
    {"Origin": "null"},
    {"Origin": "http://192.168.0.6"},                                     # another device on the LAN
], ids=["sec-fetch-cross-site", "same-site", "foreign-origin", "null-origin", "other-lan-origin"])
def test_browser_requests_from_another_site_never_reach_moonraker(server, path, headers):
    for method in ("GET", "POST"):
        status, text, _ = request(server, method, path, "192.168.0.5", headers, body="" if method == "POST" else None)
        assert status == 403 and "another site" in text and "upstream" not in text, (method, path, headers, status)


@pytest.mark.parametrize("headers", [
    {},                                                                   # slicers, apps, Home Assistant: no browser headers
    {"Origin": "http://192.168.0.5", "Sec-Fetch-Site": "same-origin"},    # Fluidd / Mainsail served by this printer
    {"Sec-Fetch-Site": "none"},                                           # typed into the address bar
], ids=["no-browser-headers", "same-origin", "direct"])
def test_the_printers_own_pages_and_non_browser_clients_still_work(server, headers):
    for method, path in (("POST", "/printer/gcode/script?script=G28"), ("GET", "/server/info"),
                         ("POST", "/server/files/upload"), ("GET", "/websocket")):
        status, text, _ = request(server, method, path, "192.168.0.5", headers, body="" if method == "POST" else None)
        assert status == 200 and text.startswith(f"upstream {method}"), (method, path, headers, status, text)


def test_same_origin_check_includes_the_port(server):
    ok = request(server, "POST", "/printer/gcode/script", "localhost:8080",
                 {"Origin": "http://localhost:8080"}, body="")                # e.g. through an SSH tunnel
    other = request(server, "POST", "/printer/gcode/script", "localhost:8080",
                    {"Origin": "http://localhost:9999"}, body="")
    assert ok[0] == 200 and other[0] == 403


def test_the_camera_and_the_ui_stay_reachable_from_other_sites(server):
    # A Home Assistant dashboard embeds the stream; a link from another site opens the UI.
    assert request(server, "GET", "/webcam/stream", "192.168.0.5", {"Sec-Fetch-Site": "cross-site"})[0] == 200
    status, text, _ = request(server, "GET", "/", "192.168.0.5", {"Sec-Fetch-Site": "cross-site"})
    assert status == 200 and text == "the web interface"


def test_the_setup_wizard_is_not_served_on_port_80(server):
    status, text, _ = request(server, "GET", "/configure/api/auth", "192.168.0.5")
    assert "upstream" not in text            # falls through to the UI's index.html, never to the configurator
