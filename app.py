#!/usr/bin/env python3
from contextlib import asynccontextmanager
from datetime import datetime, time as dtime
from pathlib import Path
from urllib.parse import quote
import json
import random
import string
import threading

import os

import requests
from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

BASE_DIR = Path(__file__).parent
STATE_FILE = Path("state.json")
MAX_LOG = 200

API_URL = "https://api.runpod.io/v2"

# v2 requires an explicit mount path; this was the v1 default.
VOLUME_MOUNT_PATH = "/workspace"
# Upstream rejects a persistent mount smaller than this.
MIN_VOLUME_GB = 10

DEFAULT_CONFIG: dict = {
    "api_key": "",
    "gpu_type": "NVIDIA GeForce RTX 4090",
    "min_ram_per_gpu": 0,
    "template": "",
    "env": "",
    "cloud": "SECURE",
    "gpu_count": 1,
    "cuda": "",
    "container_disk": 50,
    "volume": 20,
    "pod_name_base": "brutpod",
    "retry_seconds": 60,
    "proxy": "",
    "time_from": "00:00",
    "time_to": "23:59",
    "pushover_token": "",
    "pushover_user": "",
}

DEFAULT_STATUS: dict = {
    "running": False,
    "attempts": 0,
    "last_check": None,
    "last_error": None,
    "log": [],
    "success": False,
}

_lock = threading.Lock()
_stop_event = threading.Event()
_poll_thread: threading.Thread | None = None


# ---------------------------------------------------------------------------
# State helpers
# ---------------------------------------------------------------------------

def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            data = json.loads(STATE_FILE.read_text())
            stored = _migrate_config(data.get("config", {}))
            return {
                "config": {**DEFAULT_CONFIG, **stored},
                "status": {**DEFAULT_STATUS, **data.get("status", {})},
            }
        except Exception:
            pass
    return {"config": {**DEFAULT_CONFIG}, "status": {**DEFAULT_STATUS}}


def _migrate_config(stored: dict) -> dict:
    """Carry pre-v2 state forward: gpu_types was a priority list, gpu_type is one ID."""
    legacy = stored.pop("gpu_types", "")
    if legacy and not stored.get("gpu_type"):
        stored["gpu_type"] = legacy.split(",")[0].strip()
    return stored


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2, default=str))


def add_log(state: dict, msg: str) -> None:
    ts = datetime.now().strftime("%H:%M:%S")
    state["status"]["log"].insert(0, f"[{ts}] {msg}")
    state["status"]["log"] = state["status"]["log"][:MAX_LOG]


# ---------------------------------------------------------------------------
# Polling logic
# ---------------------------------------------------------------------------

def in_time_window(time_from: str, time_to: str) -> bool:
    try:
        now = datetime.now().time().replace(second=0, microsecond=0)
        t_from = dtime.fromisoformat(time_from)
        t_to = dtime.fromisoformat(time_to)
        if t_from <= t_to:
            return t_from <= now <= t_to
        return now >= t_from or now <= t_to  # overnight: e.g. 22:00–06:00
    except Exception:
        return True


def env_secret(env_var: str, fallback: str) -> str:
    """Return env var value if set, otherwise fall back to config value."""
    return os.environ.get(env_var) or fallback


def active_secrets(cfg: dict) -> dict:
    """Resolve secrets: env vars take precedence over state.json."""
    return {
        "api_key":        env_secret("RUNPOD_API_KEY",   cfg["api_key"]),
        "pushover_token": env_secret("PUSHOVER_TOKEN",   cfg["pushover_token"]),
        "pushover_user":  env_secret("PUSHOVER_USER",    cfg["pushover_user"]),
    }


def env_secrets_present() -> dict[str, bool]:
    return {
        "api_key":        bool(os.environ.get("RUNPOD_API_KEY")),
        "pushover_token": bool(os.environ.get("PUSHOVER_TOKEN")),
        "pushover_user":  bool(os.environ.get("PUSHOVER_USER")),
    }


def send_pushover(token: str, user: str, message: str) -> None:
    if not token or not user:
        return
    try:
        requests.post(
            "https://api.pushover.net/1/messages.json",
            data={"token": token, "user": user, "message": message},
            timeout=10,
        )
    except Exception:
        pass


def _random_suffix(n: int = 6) -> str:
    return "".join(random.choices(string.ascii_lowercase + string.digits, k=n))


# ---------------------------------------------------------------------------
# RunPod API v2
# ---------------------------------------------------------------------------

class ApiError(RuntimeError):
    """An error response from the v2 API.

    `fatal` marks the ones no amount of retrying fixes: a broken request, a
    rejected key, an empty balance. Polling stops on those instead of hammering
    the API once a minute forever. 400 is deliberately not fatal — on pod
    create it means either a cross-field rule violation or exhausted capacity,
    and the API gives no way to tell the two apart.
    """

    FATAL_STATUS = {401, 402, 403, 404, 422}

    def __init__(self, status: int, detail: str):
        super().__init__(f"HTTP {status}: {detail}")
        self.status = status
        self.detail = detail
        self.fatal = status in self.FATAL_STATUS


def api_request(method: str, path: str, api_key: str, proxies: dict, **kwargs):
    resp = requests.request(
        method,
        f"{API_URL}{path}",
        headers={"Authorization": f"Bearer {api_key}"},
        proxies=proxies,
        timeout=30,
        **kwargs,
    )
    if not resp.ok:
        try:
            err = resp.json()
            detail = err.get("detail") or err.get("title") or resp.text[:300]
            if err.get("errors"):
                detail = f"{detail} ({'; '.join(err['errors'])[:200]})"
        except ValueError:
            detail = resp.text[:300]
        raise ApiError(resp.status_code, detail)
    return resp.json()


def fetch_gpu_availability(gpu_type: str, cfg: dict, api_key: str, proxies: dict) -> dict:
    """Read one GPU type from the catalog, with current pod stock."""
    params = {
        "include": "AVAILABILITY",
        "product": "POD",
        "count": cfg["gpu_count"],
        "cloud": cfg["cloud"],
    }
    cuda = [c.strip() for c in cfg["cuda"].split(",") if c.strip()]
    if cuda:
        params["cudaVersions"] = ",".join(cuda)
    return api_request(
        "GET",
        f"/catalog/gpus/{quote(gpu_type, safe='')}",
        api_key,
        proxies,
        params=params,
    )


def parse_env(text: str) -> dict:
    """Parse KEY=VALUE lines into a dict, skipping blanks and # comments.

    Values are passed through verbatim so RunPod's secret placeholders
    ({{ RUNPOD_SECRET_name }}) survive intact — they are resolved when the pod
    is provisioned, not here.
    """
    env: dict = {}
    for lineno, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        key, sep, value = line.partition("=")
        key = key.strip()
        if not sep or not key:
            raise ValueError(f"Zeile {lineno}: erwartet KEY=VALUE, gefunden {line!r}")
        env[key] = value.strip()
    return env


def build_pod_payload(cfg: dict, pod_name: str) -> dict:
    gpu: dict = {"id": cfg["gpu_type"].strip(), "count": cfg["gpu_count"]}
    if cfg["min_ram_per_gpu"]:
        gpu["minRamPerGpu"] = cfg["min_ram_per_gpu"]
    cuda = [c.strip() for c in cfg["cuda"].split(",") if c.strip()]
    if cuda:
        gpu["allowedCudaVersions"] = cuda

    payload: dict = {
        "name": pod_name,
        "cloud": cfg["cloud"],
        "gpu": gpu,
        "disk": cfg["container_disk"],
    }
    if cfg["template"]:
        payload["templateId"] = cfg["template"]
    # Merged per key with the template's env, body values winning.
    env = parse_env(cfg["env"])
    if env:
        payload["env"] = env
    if cfg["volume"] >= MIN_VOLUME_GB:
        payload["mounts"] = {"persistent": {"size": cfg["volume"], "path": VOLUME_MOUNT_PATH}}
    return payload


def poll_once() -> None:
    with _lock:
        state = load_state()
        if not state["status"]["running"]:
            return
        cfg = state["config"]

        if not in_time_window(cfg["time_from"], cfg["time_to"]):
            add_log(state, f"Außerhalb des Zeitfensters ({cfg['time_from']}–{cfg['time_to']}), übersprungen")
            save_state(state)
            return

        state["status"]["attempts"] += 1
        attempt = state["status"]["attempts"]
        state["status"]["last_check"] = datetime.now().strftime("%H:%M:%S")
        add_log(state, f"Versuch {attempt}: Suche nach GPUs …")
        save_state(state)

    cfg = state["config"]
    secrets = active_secrets(cfg)
    gpu_type = cfg["gpu_type"].strip()
    proxies = {"http": cfg["proxy"], "https": cfg["proxy"]} if cfg["proxy"] else {}

    if not gpu_type:
        _fail("Kein GPU-Typ konfiguriert", fatal=True)
        return

    try:
        gpu = fetch_gpu_availability(gpu_type, cfg, secrets["api_key"], proxies)
        level = gpu.get("availability") or "NONE"
        label = gpu.get("name") or gpu_type

        if level == "NONE":
            with _lock:
                s = load_state()
                s["status"]["last_error"] = None
                add_log(s, f"  ✗ {label} — nicht verfügbar")
                save_state(s)
            return

        locations = ", ".join(dc["id"] for dc in gpu.get("dataCenters") or [])
        with _lock:
            s = load_state()
            add_log(s, f"  ✓ {label} — {level}" + (f" ({locations})" if locations else ""))
            save_state(s)

        pod_name = f"{cfg['pod_name_base']}-{_random_suffix()}"
        pod = api_request(
            "POST",
            "/pods",
            secrets["api_key"],
            proxies,
            json=build_pod_payload(cfg, pod_name),
        )

        with _lock:
            s = load_state()
            s["status"]["running"] = False
            s["status"]["success"] = True
            s["status"]["last_error"] = None
            add_log(s, f"✓ Pod erstellt! ID={pod.get('id')} Name={pod.get('name')} Status={pod.get('status')}")
            save_state(s)

        _stop_event.set()
        send_pushover(
            secrets["pushover_token"],
            secrets["pushover_user"],
            f"brutpod: GPU gebucht!\nPod {pod.get('name')} ({pod.get('id')})\nGPU: {gpu_type}",
        )

    except ApiError as e:
        _fail(str(e), fatal=e.fatal)
    except Exception as e:
        _fail(str(e))


def _fail(msg: str, fatal: bool = False) -> None:
    """Record an error; stop polling when retrying it cannot help."""
    with _lock:
        s = load_state()
        s["status"]["last_error"] = msg[:300]
        add_log(s, f"✗ {msg}")
        if fatal:
            s["status"]["running"] = False
            add_log(s, "Gestoppt — Konfiguration oder Account prüfen")
        save_state(s)
    if fatal:
        _stop_event.set()


def _polling_loop() -> None:
    poll_once()
    while not _stop_event.wait(timeout=_current_interval()):
        poll_once()


def _current_interval() -> int:
    try:
        return max(10, load_state()["config"]["retry_seconds"])
    except Exception:
        return 60


def _start_thread() -> None:
    global _poll_thread
    _stop_event.clear()
    _poll_thread = threading.Thread(target=_polling_loop, daemon=True, name="brutpod-poll")
    _poll_thread.start()


def _stop_thread() -> None:
    _stop_event.set()


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(_app: FastAPI):
    state = load_state()
    if state["status"]["running"]:
        _start_thread()
    yield
    _stop_event.set()


app = FastAPI(lifespan=lifespan)
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    state = load_state()
    return templates.TemplateResponse("index.html", {
        "request": request,
        "state": state,
        "env": env_secrets_present(),
    })


@app.post("/config", response_class=HTMLResponse)
async def save_config(
    api_key: str = Form(""),
    gpu_type: str = Form(""),
    min_ram_per_gpu: int = Form(0),
    template: str = Form(""),
    env: str = Form(""),
    cloud: str = Form("SECURE"),
    gpu_count: int = Form(1),
    cuda: str = Form(""),
    container_disk: int = Form(50),
    volume: int = Form(20),
    pod_name_base: str = Form("brutpod"),
    retry_seconds: int = Form(60),
    proxy: str = Form(""),
    time_from: str = Form("00:00"),
    time_to: str = Form("23:59"),
    pushover_token: str = Form(""),
    pushover_user: str = Form(""),
):
    try:
        parse_env(env)
    except ValueError as e:
        return HTMLResponse(f'<span class="saved error">✗ ENV: {e}</span>')

    with _lock:
        state = load_state()
        state["config"].update({
            "api_key": api_key,
            "gpu_type": gpu_type,
            "min_ram_per_gpu": min_ram_per_gpu,
            "template": template,
            "env": env,
            "cloud": cloud,
            "gpu_count": gpu_count,
            "cuda": cuda,
            "container_disk": container_disk,
            "volume": volume,
            "pod_name_base": pod_name_base,
            "retry_seconds": retry_seconds,
            "proxy": proxy,
            "time_from": time_from,
            "time_to": time_to,
            "pushover_token": pushover_token,
            "pushover_user": pushover_user,
        })
        save_state(state)
    return HTMLResponse('<span class="saved">✓ Gespeichert</span>')


@app.get("/api/gpu-types", response_class=HTMLResponse)
async def api_gpu_types():
    state = load_state()
    cfg = state["config"]
    api_key = env_secret("RUNPOD_API_KEY", cfg["api_key"])

    if not api_key:
        return HTMLResponse('<span class="api-hint">— API Key nicht gesetzt —</span>')

    try:
        data = api_request("GET", "/catalog/gpus", api_key, {})
        gpus = [g for g in data["gpus"] if g.get("secure") or g.get("community")]
        gpus.sort(key=lambda g: g.get("name", ""))

        chips = []
        for g in gpus:
            clouds = ("S" if g.get("secure") else "") + ("C" if g.get("community") else "")
            mem = g.get("memory", "?")
            gid = g["id"].replace("'", "\\'")
            chips.append(
                f'<button type="button" class="gpu-chip" '
                f'onclick="setGpuType(\'{gid}\')" title="{g["id"]}">'
                f'{g["name"]} <span class="gpu-chip-meta">{mem}GB VRAM {clouds}</span>'
                f'</button>'
            )
        return HTMLResponse('<div class="gpu-chips">' + "\n".join(chips) + "</div>")
    except Exception as e:
        return HTMLResponse(f'<span class="api-hint error">Fehler: {str(e)[:120]}</span>')


@app.get("/api/templates", response_class=HTMLResponse)
async def api_templates():
    state = load_state()
    cfg = state["config"]
    api_key = env_secret("RUNPOD_API_KEY", cfg["api_key"])

    if not api_key:
        return HTMLResponse('<option value="">— API Key nicht gesetzt —</option>')

    try:
        items = [t for t in api_request("GET", "/templates", api_key, {})["templates"]
                 if not t.get("serverless")]
    except ApiError as e:
        return HTMLResponse(f'<option value="">HTTP {e.status} — {e.detail[:80]}</option>')
    except Exception as e:
        return HTMLResponse(f'<option value="">Fehler: {str(e)[:80]}</option>')

    current = cfg["template"]
    options = ['<option value="">(kein Template)</option>']
    found = False
    for t in sorted(items, key=lambda x: (x.get("name") or "").lower()):
        tid = t.get("id", "")
        name = t.get("name") or tid
        image = t.get("image") or ""
        sel = " selected" if tid == current else ""
        if tid == current:
            found = True
        label = f"{name}  [{image}]" if image else name
        options.append(f'<option value="{tid}"{sel}>{label}</option>')

    # Preserve unknown current value so selection isn't silently lost
    if current and not found:
        options.insert(1, f'<option value="{current}" selected>{current}</option>')

    return HTMLResponse("\n".join(options))


@app.post("/start", response_class=HTMLResponse)
async def start(request: Request):
    global _poll_thread
    with _lock:
        state = load_state()
        if not active_secrets(state["config"])["api_key"]:
            return HTMLResponse('<p style="color:var(--error)">API-Key fehlt.</p>')
        state["status"].update({
            "running": True,
            "attempts": 0,
            "success": False,
            "last_error": None,
        })
        add_log(state, "Gestartet")
        save_state(state)

    _stop_thread()
    if _poll_thread and _poll_thread.is_alive():
        _poll_thread.join(timeout=2)
    _start_thread()

    state = load_state()
    return templates.TemplateResponse("_status.html", {"request": request, "state": state})


@app.post("/stop", response_class=HTMLResponse)
async def stop(request: Request):
    _stop_thread()
    with _lock:
        state = load_state()
        state["status"]["running"] = False
        add_log(state, "Gestoppt")
        save_state(state)
    return templates.TemplateResponse("_status.html", {"request": request, "state": state})


@app.get("/status", response_class=HTMLResponse)
async def status_fragment(request: Request):
    state = load_state()
    return templates.TemplateResponse("_status.html", {"request": request, "state": state})
