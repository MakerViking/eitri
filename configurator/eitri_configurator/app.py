"""HTTP API + static frontend."""
from __future__ import annotations

import ipaddress
import json
import logging
from typing import Any
from urllib.parse import urlsplit

from aiohttp import web

from . import __version__, mcuflash, options as opts, releaselock, sysinfo, workflows
from .calibrate import CalibrationError
from .context import Context
from .jobs import JobBusy
from .layer import LayerError
from .moonraker import MoonrakerError
from .settings import Settings
from .state import STEPS, StateStore

log = logging.getLogger("eitri.configurator")

CTX = web.AppKey("ctx", Context)
MOCK_MR = web.AppKey("mock_mr", web.AppRunner)

CSP = ("default-src 'self'; img-src 'self' data: blob:; style-src 'self' 'unsafe-inline'; "
       "connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'")


def jerr(status: int, message: str, **extra: Any) -> web.Response:
    return web.json_response({"ok": False, "error": message, **extra}, status=status)


async def body(request: web.Request) -> dict[str, Any]:
    if not request.can_read_body:
        return {}
    try:
        data = await request.json()
    except ValueError as exc:
        raise web.HTTPBadRequest(text=json.dumps({"ok": False, "error": "request body must be JSON"}), content_type="application/json") from exc
    if not isinstance(data, dict):
        raise web.HTTPBadRequest(text=json.dumps({"ok": False, "error": "request body must be a JSON object"}), content_type="application/json")
    return data


def _host_allowed(host: str) -> bool:
    """Refuse DNS-rebinding: only IP literals, localhost, single-label and .local names."""
    name = host.rsplit(":", 1)[0] if not host.startswith("[") else host.split("]")[0].lstrip("[")
    name = name.lower().rstrip(".")
    if not name:
        return False
    try:
        ipaddress.ip_address(name)
        return True
    except ValueError:
        pass
    return name == "localhost" or "." not in name or name.endswith(".local")


@web.middleware
async def guard(request: web.Request, handler):
    if not _host_allowed(request.host):
        return jerr(403, "unexpected Host header refused")
    # State-changing calls must come from this very origin: the configurator can flash
    # firmware, so a web page elsewhere on the LAN must not be able to drive it.
    if request.method not in ("GET", "HEAD", "OPTIONS"):
        origin = request.headers.get("Origin")
        if origin and urlsplit(origin).netloc != request.host:
            return jerr(403, "cross-origin request refused")
    try:
        resp = await handler(request)
    except web.HTTPException as exc:
        _secure(exc)
        raise
    except opts.ValidationError as exc:
        resp = jerr(422, str(exc), fields=exc.errors)
    except JobBusy as exc:
        resp = jerr(409, f"{exc.job.kind} is already running - wait for it to finish.", job=exc.job.id)
    except (CalibrationError, MoonrakerError, LayerError) as exc:
        resp = jerr(409, str(exc))
    except Exception:       # noqa: BLE001
        log.exception("unhandled error in %s %s", request.method, request.path)
        resp = jerr(500, "internal error - see the configurator log")
    _secure(resp)
    return resp


def _secure(resp: web.StreamResponse) -> None:
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("Content-Security-Policy", CSP)
    resp.headers.setdefault("Referrer-Policy", "no-referrer")


def ctx_of(request: web.Request) -> Context:
    return request.app[CTX]


def job_json(job) -> dict:
    return job.snapshot()


def public_state(ctx: Context) -> dict[str, Any]:
    st = dict(ctx.state.data)
    st["options"] = opts.validate(st["options"])
    return st


# --- handlers ---------------------------------------------------------------

async def bootstrap(request: web.Request) -> web.Response:
    ctx = ctx_of(request)
    services = await ctx.services.status()
    flavors = []
    for fid, meta in opts.FLAVORS.items():
        flavors.append({"id": fid, **meta, **releaselock.flavor_card(ctx.lock, fid)})
    active = ctx.jobs.active()
    return web.json_response({
        "app": {"version": __version__, "mock": ctx.settings.mock},
        "steps": STEPS,
        "state": public_state(ctx),
        "system": sysinfo.collect(ctx.settings, ctx.lock),
        "klarity": sysinfo.klarity_status(ctx.settings, services.get("klarity", "unknown")),
        "services": services,
        "flavors": flavors,
        "options_schema": opts.SCHEMA,
        "lock": {k: v for k, v in ctx.lock.items() if k != "firmware"} | {"source": ctx.lock["source"]},
        "active_job": job_json(active) if active else None,
    })


async def get_state(request: web.Request) -> web.Response:
    return web.json_response(public_state(ctx_of(request)))


async def put_state(request: web.Request) -> web.Response:
    ctx = ctx_of(request)
    data = await body(request)
    unknown = set(data) - StateStore.CLIENT_KEYS
    if unknown:
        return jerr(422, f"cannot set: {', '.join(sorted(unknown))}")
    upd: dict[str, Any] = {}
    if "step" in data:
        if data["step"] not in STEPS:
            return jerr(422, "unknown step")
        upd["step"] = data["step"]
    if "flavor" in data:
        if data["flavor"] not in opts.FLAVORS:
            return jerr(422, "unknown flavor")
        if data["flavor"] != ctx.state.data["flavor"]:
            upd["flash"] = None          # a different firmware flavor invalidates earlier flashes
        upd["flavor"] = data["flavor"]
    if "options" in data:
        upd["options"] = opts.validate(data["options"])
    if "hostname" in data:
        upd["hostname"] = opts.validate_hostname(data["hostname"]) if data["hostname"] else None
    if "calibration" in data:
        cal = data["calibration"]
        if not isinstance(cal, dict) or any(k not in ("zoffset", "shaper", "mesh") or v not in ("done", "skipped") for k, v in cal.items()):
            return jerr(422, "invalid calibration state")
        upd["calibration"] = {**ctx.state.data["calibration"], **cal}
    ctx.state.update(**upd)
    return web.json_response(public_state(ctx))


async def reset(request: web.Request) -> web.Response:
    ctx = ctx_of(request)
    if ctx.jobs.active():
        return jerr(409, "an operation is running")
    ctx.state.reset()
    return web.json_response(public_state(ctx))


async def complete(request: web.Request) -> web.Response:
    ctx = ctx_of(request)
    ctx.state.update(completed=True, step="done")
    return web.json_response(public_state(ctx))


async def system(request: web.Request) -> web.Response:
    ctx = ctx_of(request)
    services = await ctx.services.status()
    return web.json_response({
        "system": sysinfo.collect(ctx.settings, ctx.lock), "services": services,
        "klarity": sysinfo.klarity_status(ctx.settings, services.get("klarity", "unknown")),
    })


async def klarity(request: web.Request) -> web.Response:
    ctx = ctx_of(request)
    services = await ctx.services.status()
    return web.json_response(sysinfo.klarity_status(ctx.settings, services.get("klarity", "unknown")))


async def network(request: web.Request) -> web.Response:
    return web.json_response(await ctx_of(request).network.summary())


async def wifi_scan(request: web.Request) -> web.Response:
    return web.json_response(await ctx_of(request).network.scan())


async def wifi_connect(request: web.Request) -> web.Response:
    data = await body(request)
    res = await ctx_of(request).network.connect(str(data.get("ssid", "")), data.get("password") or None)
    return web.json_response(res, status=200 if res["ok"] else 422)


async def set_hostname(request: web.Request) -> web.Response:
    ctx = ctx_of(request)
    data = await body(request)
    res = await ctx.network.set_hostname(str(data.get("hostname", "")))
    ctx.state.update(hostname=res["hostname"])
    return web.json_response(res)


async def options_get(request: web.Request) -> web.Response:
    return web.json_response({"schema": opts.SCHEMA, "values": opts.validate(ctx_of(request).state.data["options"])})


async def options_put(request: web.Request) -> web.Response:
    ctx = ctx_of(request)
    values = opts.validate(await body(request))
    ctx.state.update(options=values)
    return web.json_response({"values": values})


def started(job) -> web.Response:
    return web.json_response({"ok": True, "job": job_json(job)}, status=202)


async def detect_start(request: web.Request) -> web.Response:
    ctx = ctx_of(request)
    data = await body(request)
    if "assign" in data:
        # Operator override of which UART is which (used when probing could not tell).
        raw = data["assign"] or {}
        if not isinstance(raw, dict) or set(raw) - set(mcuflash.ROLES) or any(not mcuflash.PORT_RE.match(str(v)) for v in raw.values()):
            return jerr(422, "assign must map mainboard/toolhead to /dev/ttyS<N> ports")
        if raw and (len(raw) != 2 or len(set(raw.values())) != 2):
            return jerr(422, "assign both boards, each to a different port")
        ctx.state.update(assign=raw or None)
    if (why := await workflows.flash_guard(ctx)):
        return jerr(409, why.replace("flashing the MCUs", "probing the boards"))
    return started(ctx.jobs.start("detect", lambda job: workflows.run_detect(ctx, job)))


async def preview(request: web.Request) -> web.Response:
    ctx = ctx_of(request)
    pv = workflows.preview(ctx)
    pv.pop("_plan")
    return web.json_response(pv)


async def apply_start(request: web.Request) -> web.Response:
    ctx = ctx_of(request)
    data = await body(request)
    if data.get("confirm") is not True:
        return jerr(422, "confirmation required")
    if ctx.state.data["detect"] is None:
        return jerr(409, "Detect the printer's boards first so the MCU ports can be written to the config.")
    return started(ctx.jobs.start("apply", lambda job: workflows.run_apply(ctx, job)))


async def flash_start(request: web.Request) -> web.Response:
    ctx = ctx_of(request)
    data = await body(request)
    if data.get("confirm") is not True:
        return jerr(422, "confirmation required")
    det = ctx.state.data["detect"]
    if not det:
        return jerr(409, "Detect the boards before flashing.")
    if not det.get("flash_ready"):
        return jerr(409, "Flashing is blocked by detection results.", blockers=det.get("blockers", []))
    roles = data.get("boards") or ["mainboard", "toolhead"]
    if not isinstance(roles, list) or not set(roles) <= {"mainboard", "toolhead"} or not roles:
        return jerr(422, "boards must be a subset of mainboard, toolhead")
    if (why := await workflows.flash_guard(ctx)):
        return jerr(409, why)
    return started(ctx.jobs.start("flash", lambda job: workflows.run_flash(ctx, job, roles)))


async def restart_service(request: web.Request) -> web.Response:
    ctx = ctx_of(request)
    data = await body(request)
    name = data.get("service")
    try:
        return web.json_response(await ctx.services.act("restart", str(name)))
    except ValueError as exc:
        return jerr(422, str(exc))


# --- calibration -------------------------------------------------------------

async def cal_status(request: web.Request) -> web.Response:
    ctx = ctx_of(request)
    ok, why = await ctx.moonraker.ready()
    out: dict[str, Any] = {"ready": ok, "message": why, "calibration": ctx.state.data["calibration"],
                           "accelerometer": opts.validate(ctx.state.data["options"])["accelerometer"]}
    if ok:
        mp = (await ctx.moonraker.query({"manual_probe": None, "configfile": ["save_config_pending"]}))
        out["manual_probe"] = mp.get("manual_probe", {})
        out["save_pending"] = mp.get("configfile", {}).get("save_config_pending", False)
    return web.json_response(out)


async def cal_zoffset(request: web.Request) -> web.Response:
    ctx = ctx_of(request)
    action = request.match_info["action"]
    cal = ctx.calibrator
    if action == "start":
        return started(ctx.jobs.start("calibrate.zoffset", cal.zoffset_start))
    data = await body(request)
    if action == "nudge":
        return web.json_response(await cal.zoffset_nudge(data.get("delta", 0)))
    if action == "accept":
        return web.json_response(await cal.zoffset_accept())
    if action == "abort":
        return web.json_response(await cal.zoffset_abort())
    return jerr(404, "unknown action")


async def cal_shaper(request: web.Request) -> web.Response:
    ctx = ctx_of(request)
    if opts.validate(ctx.state.data["options"])["accelerometer"] == "none":
        return jerr(409, "No accelerometer is configured. Enable one in Options to calibrate the input shaper.")
    return started(ctx.jobs.start("calibrate.shaper", ctx.calibrator.shaper))


async def cal_mesh(request: web.Request) -> web.Response:
    ctx = ctx_of(request)
    if request.method == "GET":
        return web.json_response(await ctx.calibrator.mesh_state())
    return started(ctx.jobs.start("calibrate.mesh", ctx.calibrator.mesh))


async def cal_save(request: web.Request) -> web.Response:
    ctx = ctx_of(request)
    return started(ctx.jobs.start("calibrate.save", ctx.calibrator.save))


# --- jobs ---------------------------------------------------------------------

async def job_get(request: web.Request) -> web.Response:
    job = ctx_of(request).jobs.get(request.match_info["id"])
    if not job:
        return jerr(404, "no such job")
    return web.json_response({**job_json(job), "events": job.events[-400:]})


async def job_latest(request: web.Request) -> web.Response:
    job = ctx_of(request).jobs.last_by_kind.get(request.match_info["kind"])
    if not job:
        return jerr(404, "no such job")
    return web.json_response({**job_json(job), "events": job.events[-400:]})


async def job_events(request: web.Request) -> web.StreamResponse:
    job = ctx_of(request).jobs.get(request.match_info["id"])
    if not job:
        return jerr(404, "no such job")
    try:
        since = int(request.query.get("since") or request.headers.get("Last-Event-ID") or 0)
    except ValueError:
        since = 0
    resp = web.StreamResponse(headers={
        "Content-Type": "text/event-stream", "Cache-Control": "no-cache", "X-Accel-Buffering": "no",
    })
    await resp.prepare(request)
    await resp.write(b"retry: 1500\n\n")
    try:
        async for ev in job.stream(since, heartbeat=10):
            if ev is None:
                await resp.write(b": keepalive\n\n")
            else:
                await resp.write(f"id: {ev['seq']}\ndata: {json.dumps(ev)}\n\n".encode())
        await resp.write_eof()
    except (ConnectionResetError, ConnectionError):
        pass
    return resp


# --- static ---------------------------------------------------------------------

async def index(request: web.Request) -> web.StreamResponse:
    return web.FileResponse(ctx_of(request).settings.web_dir / "index.html", headers={"Cache-Control": "no-cache"})


@web.middleware
async def static_cache(request: web.Request, handler):
    resp = await handler(request)
    if request.path.startswith("/fonts/"):
        resp.headers["Cache-Control"] = "public, max-age=31536000, immutable"
    elif not request.path.startswith("/api/"):
        resp.headers.setdefault("Cache-Control", "no-cache")
    return resp


def create_app(settings: Settings) -> web.Application:
    ctx = Context(settings=settings, state=StateStore(settings.state_file))
    app = web.Application(middlewares=[guard, static_cache], client_max_size=64 * 1024)
    app[CTX] = ctx
    r = app.router
    r.add_get("/api/bootstrap", bootstrap)
    r.add_get("/api/state", get_state)
    r.add_put("/api/state", put_state)
    r.add_post("/api/reset", reset)
    r.add_post("/api/complete", complete)
    r.add_get("/api/system", system)
    r.add_get("/api/klarity", klarity)
    r.add_get("/api/network", network)
    r.add_get("/api/network/wifi", wifi_scan)
    r.add_post("/api/network/wifi", wifi_connect)
    r.add_post("/api/network/hostname", set_hostname)
    r.add_get("/api/options", options_get)
    r.add_put("/api/options", options_put)
    r.add_post("/api/detect", detect_start)
    r.add_post("/api/preview", preview)
    r.add_post("/api/apply", apply_start)
    r.add_post("/api/flash", flash_start)
    r.add_post("/api/services/restart", restart_service)
    r.add_get("/api/calibrate/status", cal_status)
    r.add_post("/api/calibrate/zoffset/{action}", cal_zoffset)
    r.add_post("/api/calibrate/shaper", cal_shaper)
    r.add_route("*", "/api/calibrate/mesh", cal_mesh)
    r.add_post("/api/calibrate/save", cal_save)
    r.add_get("/api/jobs/{id}", job_get)
    r.add_get("/api/jobs/{id}/events", job_events)
    r.add_get("/api/jobs/latest/{kind}", job_latest)
    r.add_get("/", index)
    r.add_static("/", settings.web_dir, follow_symlinks=False)

    async def on_startup(app: web.Application) -> None:
        if settings.mock:
            from .mock import moonraker as mock_mr
            app[MOCK_MR], ctx.settings.moonraker_url = await mock_mr.start(settings)
            ctx.moonraker.base = ctx.settings.moonraker_url

    async def on_cleanup(app: web.Application) -> None:
        await ctx.jobs.shutdown()
        await ctx.moonraker.close()
        if MOCK_MR in app:
            await app[MOCK_MR].cleanup()

    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    return app
