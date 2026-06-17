#!/usr/bin/env python3
"""
Sucht nach verfügbaren GPUs bei Simplepod und deployt ein Template.
Wiederholt die Suche jede Minute, bis eine GPU gefunden wird.
"""

import argparse
import sys
import time
import requests

BASE_URL = "https://api.simplepod.ai"

RESET  = "\033[0m"
BOLD   = "\033[1m"
DIM    = "\033[2m"
RED    = "\033[31m"
GREEN  = "\033[32m"
YELLOW = "\033[33m"
CYAN   = "\033[36m"
WHITE  = "\033[97m"


def log(msg: str = "") -> None:
    print(msg, flush=True)


def bell() -> None:
    sys.stdout.write("\a")
    sys.stdout.flush()


def check_gpu_availability(args: argparse.Namespace) -> list[dict]:
    params = {"rentalStatus": "active"}
    if args.gpu_model:
        params["gpuModel"] = args.gpu_model
    if args.gpu_count > 1:
        params["gpuCount[gte]"] = args.gpu_count
    if args.cuda:
        params["gpuCudaVer"] = args.cuda[0]

    resp = requests.get(
        f"{BASE_URL}/instances/market/list",
        params=params,
        headers={"X-AUTH-TOKEN": args.api_key},
        proxies=args.proxies,
        timeout=30,
    )
    resp.raise_for_status()
    items = resp.json()

    available = []
    seen_models: set[str] = set()
    for item in items:
        model = item.get("gpuModel", "?")
        count = item.get("gpuCount", 1)
        cuda = item.get("gpuCudaVer", "?")
        price = item.get("pricePerGpu")
        price_str = f"${price:.3f}/h" if price is not None else "?"
        if model not in seen_models:
            log(f"  {GREEN}✓{RESET} {WHITE}{model}{RESET} x{count} {DIM}CUDA {cuda}{RESET} — {price_str}")
            seen_models.add(model)
        available.append(item)

    return available


def create_instance(args: argparse.Namespace, market_item: dict) -> dict:
    market_iri = market_item.get("instanceMarket") or f"/instances/market/{market_item['id']}"
    payload = {
        "gpuCount": args.gpu_count,
        "instanceMarket": market_iri,
        "instanceTemplate": f"/instances/templates/{args.template}",
    }

    resp = requests.post(
        f"{BASE_URL}/instances",
        json=payload,
        headers={
            "X-AUTH-TOKEN": args.api_key,
            "Content-Type": "application/json",
        },
        proxies=args.proxies,
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Sucht nach verfügbaren Simplepod GPUs und deployt ein Template."
    )
    parser.add_argument("--api-key", required=True, metavar="KEY",
                        help="Simplepod API Key (X-AUTH-TOKEN)")
    parser.add_argument("--gpu-model", default="", metavar="MODEL",
                        help="GPU-Modell-Filter, z.B. 'RTX 4090' (leer = alle)")
    parser.add_argument("--template", required=True, metavar="ID",
                        help="Template-ID aus /instances/templates/list")
    parser.add_argument("--gpu-count", type=int, default=1, metavar="N",
                        help="Anzahl GPUs (Standard: 1)")
    parser.add_argument("--cuda", nargs="+", metavar="VER",
                        help="CUDA-Version (z.B. 12.8)")
    parser.add_argument("--retry", type=int, default=60, metavar="SEC",
                        help="Wartezeit in Sekunden zwischen Versuchen (Standard: 60)")
    parser.add_argument("--proxy", default="", metavar="URL",
                        help="HTTP/HTTPS Proxy URL (optional)")

    args = parser.parse_args()
    args.proxies = {"http": args.proxy, "https": args.proxy} if args.proxy else {}
    return args


def main():
    args = parse_args()

    log(f"{BOLD}{CYAN}Simplepod GPU Deployer{RESET}")
    log(f"  {DIM}GPU-Filter     :{RESET} {args.gpu_model or '(alle)'}")
    log(f"  {DIM}Template-ID    :{RESET} {args.template}")
    log(f"  {DIM}CUDA-Version   :{RESET} {', '.join(args.cuda) if args.cuda else '(alle)'}")
    log(f"  {DIM}GPU-Anzahl     :{RESET} {args.gpu_count}")
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
            market_item = available[0]
            model = market_item.get("gpuModel", "?")
            log(f"\n{GREEN}{BOLD}GPU gefunden:{RESET} {model} (market/{market_item['id']})")
            log(f"{CYAN}Erstelle Instanz ...{RESET}")
            try:
                instance = create_instance(args, market_item)
                log()
                log(f"{GREEN}{BOLD}✓ Instanz erfolgreich erstellt!{RESET}")
                log(f"  {DIM}ID      :{RESET}  {instance.get('id')}")
                log(f"  {DIM}Support :{RESET}  {instance.get('supportId')}")
                log(f"  {DIM}Status  :{RESET}  {instance.get('status')}")
                bell()
                break
            except requests.HTTPError as e:
                log(f"  {RED}✗ Fehler beim Erstellen:{RESET} {e.response.status_code} {e.response.text}")
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
