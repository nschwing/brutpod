#!/usr/bin/env python3
from contextlib import asynccontextmanager
from datetime import datetime, time as dtime
from pathlib import Path
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

GRAPHQL_URL = "https://api.runpod.io/graphql"
REST_URL = "https://rest.runpod.io/v1"

DEFAULT_CONFIG: dict = {
    "api_key": "",
    "gpu_types": "NVIDIA GeForce RTX 4090",
    "template": "",
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
            return {
                "config": {**DEFAULT_CONFIG, **data.get("config", {})},
                "status": {**DEFAULT_STATUS, **data.get("status", {})},
            }
        except Exception:
            pass
    return {"config": {**DEFAULT_CONFIG}, "status": {**DEFAULT_STATUS}}


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
    gpu_types = [g.strip() for g in cfg["gpu_types"].split(",") if g.strip()]
    cuda_versions = [c.strip() for c in cfg["cuda"].split(",") if c.strip()] if cfg["cuda"] else []
    proxies = {"http": cfg["proxy"], "https": cfg["proxy"]} if cfg["proxy"] else {}
    secure = cfg["cloud"] == "SECURE"

    try:
        query = """
        query GpuTypes($input: GpuLowestPriceInput) {
          gpuTypes {
            id displayName memoryInGb secureCloud communityCloud
            lowestPrice(input: $input) {
              minimumBidPrice uninterruptablePrice stockStatus
            }
          }
        }
        """
        resp = requests.post(
            GRAPHQL_URL,
            json={"query": query, "variables": {"input": {"gpuCount": cfg["gpu_count"], "secureCloud": secure}}},
            headers={"Authorization": f"Bearer {secrets['api_key']}"},
            proxies=proxies,
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        if "errors" in data:
            raise RuntimeError(f"GraphQL: {data['errors']}")

        available: list[str] = []
        for gpu in data["data"]["gpuTypes"]:
            if gpu["id"] not in gpu_types:
                continue
            in_cloud = gpu["secureCloud"] if secure else gpu["communityCloud"]
            if not in_cloud:
                continue
            stock = (gpu.get("lowestPrice") or {}).get("stockStatus")
            if stock:
                available.append(gpu["id"])
                with _lock:
                    s = load_state()
                    add_log(s, f"  ✓ {gpu['displayName']} — {stock}")
                    save_state(s)
            else:
                with _lock:
                    s = load_state()
                    add_log(s, f"  ✗ {gpu['displayName']} — nicht verfügbar")
                    save_state(s)

        if not available:
            with _lock:
                s = load_state()
                s["status"]["last_error"] = None
                add_log(s, "Keine passenden GPUs verfügbar")
                save_state(s)
            return

        # Create pod
        ordered_ids = [g for g in gpu_types if g in available]
        pod_name = f"{cfg['pod_name_base']}-{_random_suffix()}"
        payload: dict = {
            "name": pod_name,
            "cloudType": cfg["cloud"],
            "gpuTypeIds": ordered_ids,
            "gpuCount": cfg["gpu_count"],
            "containerDiskInGb": cfg["container_disk"],
            "volumeInGb": cfg["volume"],
            "gpuTypePriority": "custom",
        }
        if cfg["template"]:
            payload["templateId"] = cfg["template"]
        if cuda_versions:
            payload["allowedCudaVersions"] = cuda_versions

        resp2 = requests.post(
            f"{REST_URL}/pods",
            json=payload,
            headers={"Authorization": f"Bearer {secrets['api_key']}"},
            proxies=proxies,
            timeout=30,
        )
        resp2.raise_for_status()
        pod = resp2.json()

        with _lock:
            s = load_state()
            s["status"]["running"] = False
            s["status"]["success"] = True
            s["status"]["last_error"] = None
            add_log(s, f"✓ Pod erstellt! ID={pod.get('id')} Name={pod.get('name')} Status={pod.get('desiredStatus')}")
            save_state(s)

        _stop_event.set()
        send_pushover(
            secrets["pushover_token"],
            secrets["pushover_user"],
            f"brutpod: GPU gebucht!\nPod {pod.get('name')} ({pod.get('id')})\nGPUs: {', '.join(ordered_ids)}",
        )

    except requests.HTTPError as e:
        err = f"HTTP {e.response.status_code}: {e.response.text[:300]}"
        with _lock:
            s = load_state()
            s["status"]["last_error"] = err
            add_log(s, f"✗ {err}")
            save_state(s)
    except Exception as e:
        with _lock:
            s = load_state()
            s["status"]["last_error"] = str(e)[:300]
            add_log(s, f"✗ {e}")
            save_state(s)


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
    gpu_types: str = Form(""),
    template: str = Form(""),
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
    with _lock:
        state = load_state()
        state["config"].update({
            "api_key": api_key,
            "gpu_types": gpu_types,
            "template": template,
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
        query = "{ gpuTypes { id displayName memoryInGb secureCloud communityCloud } }"
        resp = requests.post(
            GRAPHQL_URL,
            json={"query": query},
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        if "errors" in data:
            raise RuntimeError(data["errors"])

        gpus = data["data"]["gpuTypes"]
        gpus = [g for g in gpus if g.get("secureCloud") or g.get("communityCloud")]
        gpus.sort(key=lambda g: g.get("displayName", ""))

        chips = []
        for g in gpus:
            clouds = ("S" if g.get("secureCloud") else "") + ("C" if g.get("communityCloud") else "")
            mem = g.get("memoryInGb", "?")
            gid = g["id"].replace("'", "\\'")
            chips.append(
                f'<button type="button" class="gpu-chip" '
                f'onclick="addGpuType(\'{gid}\')" title="{g["id"]}">'
                f'{g["displayName"]} <span class="gpu-chip-meta">{mem}GB {clouds}</span>'
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
        resp = requests.get(
            f"{REST_URL}/templates",
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=15,
        )
        resp.raise_for_status()
        items = resp.json()
    except requests.HTTPError as e:
        return HTMLResponse(f'<option value="">HTTP {e.response.status_code}: {e.response.text[:80]}</option>')
    except Exception as e:
        return HTMLResponse(f'<option value="">Fehler: {str(e)[:80]}</option>')

    current = cfg["template"]
    options = ['<option value="">(kein Template)</option>']
    found = False
    for t in sorted(items, key=lambda x: (x.get("name") or "").lower()):
        tid = t.get("id", "")
        name = t.get("name") or tid
        image = t.get("imageName") or ""
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
