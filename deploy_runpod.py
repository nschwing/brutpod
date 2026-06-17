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
import requests

GRAPHQL_URL = "https://api.runpod.io/graphql"
REST_URL = "https://rest.runpod.io/v1"

# --- Farben (ANSI) ---
RESET  = "\033[0m"
BOLD   = "\033[1m"
DIM    = "\033[2m"
RED    = "\033[31m"
GREEN  = "\033[32m"
YELLOW = "\033[33m"
CYAN   = "\033[36m"
WHITE  = "\033[97m"

STOCK_COLOR = {"High": GREEN, "Medium": YELLOW, "Low": YELLOW}


def log(msg: str = "") -> None:
    print(msg, flush=True)


def bell() -> None:
    sys.stdout.write("\a")
    sys.stdout.flush()


def check_gpu_availability(args: argparse.Namespace) -> list[str]:
    secure = args.cloud == "SECURE"
    query = """
    query GpuTypes($input: GpuLowestPriceInput) {
      gpuTypes {
        id
        displayName
        memoryInGb
        secureCloud
        communityCloud
        lowestPrice(input: $input) {
          minimumBidPrice
          uninterruptablePrice
          stockStatus
        }
      }
    }
    """
    variables = {"input": {"gpuCount": args.gpu_count, "secureCloud": secure}}
    resp = requests.post(
        GRAPHQL_URL,
        json={"query": query, "variables": variables},
        headers={"Authorization": f"Bearer {args.api_key}"},
        proxies=args.proxies,
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()

    if "errors" in data:
        raise RuntimeError(f"GraphQL Fehler: {data['errors']}")

    available = []
    for gpu in data["data"]["gpuTypes"]:
        if gpu["id"] not in args.gpu_types:
            continue
        in_cloud = gpu["secureCloud"] if secure else gpu["communityCloud"]
        if not in_cloud:
            continue
        price_info = gpu.get("lowestPrice") or {}
        stock = price_info.get("stockStatus")
        if stock:
            color = STOCK_COLOR.get(stock, GREEN)
            log(f"  {GREEN}✓{RESET} {WHITE}{gpu['displayName']}{RESET} {DIM}({gpu['id']}){RESET} — Stock: {color}{BOLD}{stock}{RESET}")
            available.append(gpu["id"])
        else:
            log(f"  {DIM}✗ {gpu['displayName']} ({gpu['id']}) — nicht verfügbar{RESET}")

    return available


def create_pod(args: argparse.Namespace, available_gpu_ids: list[str]) -> dict:
    ordered_ids = [g for g in args.gpu_types if g in available_gpu_ids]
    payload = {
        "name": args.pod_name,
        "cloudType": args.cloud,
        "gpuTypeIds": ordered_ids,
        "gpuCount": args.gpu_count,
        "containerDiskInGb": args.container_disk,
        "volumeInGb": args.volume,
        "gpuTypePriority": "custom",
    }
    if args.template:
        payload["templateId"] = args.template
    if args.cuda:
        payload["allowedCudaVersions"] = args.cuda

    resp = requests.post(
        f"{REST_URL}/pods",
        json=payload,
        headers={"Authorization": f"Bearer {args.api_key}"},
        proxies=args.proxies,
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Sucht nach verfügbaren RunPod GPUs und deployt ein Template."
    )
    parser.add_argument("--api-key", required=True, metavar="KEY",
                        help="RunPod API Key")
    parser.add_argument("--gpu-types", required=True, metavar="GPU", nargs="+",
                        help="GPU-Typen in Prioritätsreihenfolge, z.B. 'NVIDIA GeForce RTX 4090'")
    parser.add_argument("--template", default="", metavar="ID",
                        help="Template-ID (optional)")
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
    log(f"  {DIM}Gesuchte GPUs  :{RESET} {', '.join(args.gpu_types)}")
    log(f"  {DIM}Cloud-Typ      :{RESET} {args.cloud}")
    log(f"  {DIM}Template-ID    :{RESET} {args.template or '(keins)'}")
    log(f"  {DIM}CUDA-Versionen :{RESET} {', '.join(args.cuda) if args.cuda else '(alle)'}")
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
        except Exception as e:
            log(f"  {RED}✗ Fehler bei GPU-Abfrage:{RESET} {e}")
            available = []

        if available:
            log(f"\n{GREEN}{BOLD}GPU(s) gefunden:{RESET} {', '.join(available)}")
            log(f"{CYAN}Erstelle Pod ...{RESET}")
            try:
                pod = create_pod(args, available)
                log()
                log(f"{GREEN}{BOLD}✓ Pod erfolgreich erstellt!{RESET}")
                log(f"  {DIM}Pod-ID :{RESET}  {pod.get('id')}")
                log(f"  {DIM}Name   :{RESET}  {pod.get('name')}")
                log(f"  {DIM}Status :{RESET}  {pod.get('desiredStatus')}")
                bell()
                break
            except requests.HTTPError as e:
                log(f"  {RED}✗ Fehler beim Erstellen des Pods:{RESET} {e.response.status_code} {e.response.text}")
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
