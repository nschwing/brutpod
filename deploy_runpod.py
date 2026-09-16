#!/usr/bin/env python3
"""
Sucht nach verfügbaren GPUs bei RunPod und deployt ein Template.
Wiederholt die Suche jede Minute, bis eine GPU gefunden wird.
"""

import argparse
import random
import string
import sys
import time
from urllib.parse import quote

import requests

API_URL = "https://api.runpod.io/v2"

# v2 requires an explicit mount path; this was the v1 default.
VOLUME_MOUNT_PATH = "/workspace"
# Upstream rejects a persistent mount smaller than this.
MIN_VOLUME_GB = 10

# --- Farben (ANSI) ---
RESET  = "\033[0m"
BOLD   = "\033[1m"
DIM    = "\033[2m"
RED    = "\033[31m"
GREEN  = "\033[32m"
YELLOW = "\033[33m"
CYAN   = "\033[36m"
WHITE  = "\033[97m"

STOCK_COLOR = {"HIGH": GREEN, "MEDIUM": YELLOW, "LOW": YELLOW}

# Retrying these never helps: a broken request, a rejected key, an empty balance.
FATAL_STATUS = {401, 402, 403, 404, 422}


def log(msg: str = "") -> None:
    print(msg, flush=True)


def bell() -> None:
    sys.stdout.write("\a")
    sys.stdout.flush()


class ApiError(RuntimeError):
    def __init__(self, status: int, detail: str):
        super().__init__(f"HTTP {status}: {detail}")
        self.status = status
        self.fatal = status in FATAL_STATUS


def api_request(method: str, path: str, args: argparse.Namespace, **kwargs) -> dict:
    resp = requests.request(
        method,
        f"{API_URL}{path}",
        headers={"Authorization": f"Bearer {args.api_key}"},
        proxies=args.proxies,
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


def check_gpu_availability(args: argparse.Namespace) -> bool:
    params = {
        "include": "AVAILABILITY",
        "product": "POD",
        "count": args.gpu_count,
        "cloud": args.cloud,
    }
    if args.cuda:
        params["cudaVersions"] = ",".join(args.cuda)

    gpu = api_request(
        "GET", f"/catalog/gpus/{quote(args.gpu_type, safe='')}", args, params=params
    )
    level = gpu.get("availability") or "NONE"
    name = gpu.get("name") or args.gpu_type

    if level == "NONE":
        log(f"  {DIM}✗ {name} ({args.gpu_type}) — nicht verfügbar{RESET}")
        return False

    locations = ", ".join(dc["id"] for dc in gpu.get("dataCenters") or [])
    color = STOCK_COLOR.get(level, GREEN)
    log(f"  {GREEN}✓{RESET} {WHITE}{name}{RESET} {DIM}({args.gpu_type}){RESET} — Stock: {color}{BOLD}{level}{RESET}"
        + (f" {DIM}[{locations}]{RESET}" if locations else ""))
    return True


def create_pod(args: argparse.Namespace) -> dict:
    gpu = {"id": args.gpu_type, "count": args.gpu_count}
    if args.min_ram:
        gpu["minRamPerGpu"] = args.min_ram
    if args.cuda:
        gpu["allowedCudaVersions"] = args.cuda

    payload = {
        "name": args.pod_name,
        "cloud": args.cloud,
        "gpu": gpu,
        "disk": args.container_disk,
    }
    if args.template:
        payload["templateId"] = args.template
    # Merged per key with the template's env, body values winning.
    if args.env:
        payload["env"] = dict(args.env)
    if args.volume >= MIN_VOLUME_GB:
        payload["mounts"] = {"persistent": {"size": args.volume, "path": VOLUME_MOUNT_PATH}}

    return api_request("POST", "/pods", args, json=payload)


def env_pair(raw: str) -> tuple[str, str]:
    """Parse one KEY=VALUE argument.

    The value is passed through verbatim so RunPod's secret placeholders
    ({{ RUNPOD_SECRET_name }}) survive intact — they are resolved when the pod
    is provisioned, not here.
    """
    key, sep, value = raw.partition("=")
    key = key.strip()
    if not sep or not key:
        raise argparse.ArgumentTypeError(f"erwartet KEY=VALUE, gefunden {raw!r}")
    return key, value.strip()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Sucht nach verfügbaren RunPod GPUs und deployt ein Template."
    )
    parser.add_argument("--api-key", required=True, metavar="KEY",
                        help="RunPod API Key")
    parser.add_argument("--gpu-type", required=True, metavar="GPU",
                        help="GPU-Typ, z.B. 'NVIDIA GeForce RTX 4090'")
    parser.add_argument("--min-ram", type=int, default=0, metavar="GB",
                        help="Minimaler Host-RAM in GB pro GPU (kein VRAM; Standard: egal)")
    parser.add_argument("--template", default="", metavar="ID",
                        help="Template-ID (optional)")
    parser.add_argument("--env", type=env_pair, action="append", default=[], metavar="KEY=VALUE",
                        help="ENV-Override, mehrfach angebbar; überschreibt den Template-Wert "
                             "dieses Keys. Secrets: 'KEY={{ RUNPOD_SECRET_name }}'")
    parser.add_argument("--cloud", default="SECURE", choices=["SECURE", "COMMUNITY"],
                        help="Cloud-Typ (Standard: SECURE)")
    parser.add_argument("--gpu-count", type=int, default=1, metavar="N",
                        help="Anzahl GPUs pro Pod (Standard: 1)")
    parser.add_argument("--cuda", nargs="+", metavar="VER",
                        help="Erlaubte CUDA-Versionen, z.B. 12.8 12.9")
    parser.add_argument("--container-disk", type=int, default=50, metavar="GB",
                        help="Container-Disk in GB (Standard: 50)")
    parser.add_argument("--volume", type=int, default=20, metavar="GB",
                        help="Volume in GB (Standard: 20)")
    parser.add_argument("--name", default="brutpod", dest="pod_base", metavar="NAME",
                        help="Pod-Basisname (Standard: brutpod)")
    parser.add_argument("--retry", type=int, default=60, metavar="SEC",
                        help="Wartezeit in Sekunden zwischen Versuchen (Standard: 60)")
    parser.add_argument("--proxy", default="", metavar="URL",
                        help="HTTP/HTTPS Proxy URL (optional)")

    args = parser.parse_args()
    suffix = "".join(random.choices(string.ascii_lowercase + string.digits, k=6))
    args.pod_name = f"{args.pod_base}-{suffix}"
    args.proxies = {"http": args.proxy, "https": args.proxy} if args.proxy else {}
    return args


def main():
    args = parse_args()

    log(f"{BOLD}{CYAN}RunPod GPU Deployer{RESET}")
    log(f"  {DIM}Gesuchte GPU   :{RESET} {args.gpu_type}")
    log(f"  {DIM}Cloud-Typ      :{RESET} {args.cloud}")
    log(f"  {DIM}Template-ID    :{RESET} {args.template or '(keins)'}")
    log(f"  {DIM}CUDA-Versionen :{RESET} {', '.join(args.cuda) if args.cuda else '(alle)'}")
    log(f"  {DIM}Min. RAM/GPU   :{RESET} {f'{args.min_ram} GB' if args.min_ram else '(egal)'}")
    # Keys only — a value may be a literal secret typed on the command line.
    log(f"  {DIM}ENV Overrides  :{RESET} {', '.join(k for k, _ in args.env) or '(keine)'}")
    log(f"  {DIM}Pod-Name       :{RESET} {args.pod_name}")
    log(f"  {DIM}Retry-Intervall:{RESET} {args.retry}s")
    log(f"  {DIM}Proxy          :{RESET} {args.proxy or '(keiner)'}")
    log()

    attempt = 0
    while True:
        attempt += 1
        log(f"{CYAN}[Versuch {attempt}]{RESET} Suche nach verfügbaren GPUs ...")
        try:
            available = check_gpu_availability(args)
        except ApiError as e:
            log(f"  {RED}✗ Fehler bei GPU-Abfrage:{RESET} {e}")
            if e.fatal:
                sys.exit(1)
            available = False
        except Exception as e:
            log(f"  {RED}✗ Fehler bei GPU-Abfrage:{RESET} {e}")
            available = False

        if available:
            log(f"\n{CYAN}Erstelle Pod ...{RESET}")
            try:
                pod = create_pod(args)
                log()
                log(f"{GREEN}{BOLD}✓ Pod erfolgreich erstellt!{RESET}")
                log(f"  {DIM}Pod-ID :{RESET}  {pod.get('id')}")
                log(f"  {DIM}Name   :{RESET}  {pod.get('name')}")
                log(f"  {DIM}Status :{RESET}  {pod.get('status')}")
                bell()
                break
            except ApiError as e:
                log(f"  {RED}✗ Fehler beim Erstellen des Pods:{RESET} {e}")
                if e.fatal:
                    sys.exit(1)
                log(f"  Nächster Versuch in {args.retry}s ...")
        else:
            log(f"  {YELLOW}Keine GPUs verfügbar.{RESET} Nächster Versuch in {args.retry}s ...")

        time.sleep(args.retry)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log(f"\n{YELLOW}Abgebrochen.{RESET}")
        sys.exit(0)
