#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "httpx",
#   "rich",
# ]
# ///
"""
IKEA Chile Stock Monitor
========================
Monitors IKEA Chile product availability and notifies you via Telegram
when items are back in stock.

Usage:
    chmod +x ikea_stock_monitor.py
    ./ikea_stock_monitor.py 30623912 40623913 ...

    # Custom interval:
    ./ikea_stock_monitor.py --interval 60 30623912

    # Single check and exit (useful for cron):
    ./ikea_stock_monitor.py --once 30623912

Configuration (edit the CONFIG section below or use env vars):
    IKEA_TELEGRAM_TOKEN   Telegram bot token
    IKEA_TELEGRAM_CHAT_ID Telegram chat ID
"""

import argparse
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

import httpx
from rich import print as rprint
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

# ──────────────────────────────────────────────
# ❶  CONFIGURATION  — edit here or use env vars
# ──────────────────────────────────────────────
CONFIG = {
    # Check interval in minutes (overridable via --interval)
    "interval_minutes": 360,  # 6 hours
    # IKEA country / language
    "country": "cl",
    "language": "es",
    # ── Telegram ───────────────────────────────
    # 1. Create a bot via @BotFather and get your token
    # 2. Get your chat_id by messaging @userinfobot
    "telegram_token": os.getenv("IKEA_TELEGRAM_TOKEN", ""),
    "telegram_chat_id": os.getenv("IKEA_TELEGRAM_CHAT_ID", ""),
}
# ──────────────────────────────────────────────

console = Console()

# Ingka availability API — the same one the IKEA website calls.
# "ru" in the path stands for "retail unit" (not Russia), it's a fixed path segment.
AVAILABILITY_URL = (
    "https://api.ingka.ikea.com/cia/availabilities/ru/{country}"
    "?itemNos={item_no}&expand=StoresList,Restocks"
)
INGKA_CLIENT_ID = "b6c117e5-ae61-4ef5-b4cc-e0b1e37f0631"

PRODUCT_URL = "https://www.ikea.com/{country}/{lang}/p/-{item_no}/"
STATE_FILE = Path.home() / ".ikea_stock_monitor_state.json"
PRODUCT_NAME_URL = "https://api.ingka.ikea.com/product/ingka/{country}/{item_no}"


# ── Product name lookup ───────────────────────────────────────────────────────


def fetch_product_name(item_no: str, country: str, language: str) -> str:
    """Fallback: scrape the product name from the IKEA product page <title>."""
    url = f"https://www.ikea.com/{country}/{language}/p/-{item_no}/"
    try:
        resp = httpx.get(
            url,
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=15,
            follow_redirects=True,
        )
        # Title format: "NAME Description, ... - IKEA Chile"
        import re

        match = re.search(r"<title>([^<]+)</title>", resp.text)
        if match:
            title = match.group(1).strip()
            # Strip the trailing " - IKEA Chile" part
            title = re.sub(r"\s*-\s*IKEA.*$", "", title).strip()
            # Truncate at comma to keep it short
            short = title.split(",")[0].strip()
            return short or item_no
    except Exception:
        pass
    return item_no


# ── Stock check ───────────────────────────────────────────────────────────────


def check_stock(item_no: str, country: str) -> dict | None:
    """
    Call IKEA's Ingka availability API.
    Returns dict with: available (bool), stock (int), probability (str), restock_date (str|None)
    Returns None on error.
    """
    url = AVAILABILITY_URL.format(country=country, item_no=item_no)
    headers = {
        "Accept": "application/json;version=2",
        "X-Client-ID": INGKA_CLIENT_ID,
    }
    try:
        resp = httpx.get(url, headers=headers, timeout=15, follow_redirects=True)
        resp.raise_for_status()
    except httpx.HTTPStatusError as e:
        console.print(f"[red]HTTP error for {item_no}: {e}[/red]")
        return None
    except httpx.HTTPError as e:
        console.print(f"[red]Network error for {item_no}: {e}[/red]")
        return None

    try:
        data = resp.json()
    except Exception as e:
        console.print(f"[red]JSON parse error for {item_no}: {e}[/red]")
        return None

    availabilities = data.get("availabilities", [])
    if not availabilities:
        return {
            "available": False,
            "stock": 0,
            "probability": "UNKNOWN",
            "restock_date": None,
        }

    # Aggregate across all entries (stores + online)
    total_stock = 0
    best_prob = "OUT_OF_STOCK"
    restock_date = None

    prob_rank = {
        "HIGH_IN_STOCK": 3,
        "MEDIUM_IN_STOCK": 2,
        "LOW_IN_STOCK": 1,
        "OUT_OF_STOCK": 0,
    }

    for entry in availabilities:
        buying = entry.get("buyingOption", {})

        # Online / home delivery
        for key in ("homeDelivery", "cashCarry", "clickCollect"):
            section = buying.get(key, {})
            avail = section.get("availability", {})
            qty = avail.get("quantity", 0) or 0
            total_stock += qty

            prob_obj = avail.get("probability", {})
            # probability can be nested under "thisDay" or directly
            msg_type = (
                prob_obj.get("thisDay", {}).get("messageType")
                or prob_obj.get("messageType")
                or ""
            )
            if prob_rank.get(msg_type, -1) > prob_rank.get(best_prob, 0):
                best_prob = msg_type

            # Restock date
            restocks = avail.get("restocks", [])
            if restocks and not restock_date:
                restock_date = restocks[0].get("earliestDate")

    available = total_stock > 0 or best_prob in ("HIGH_IN_STOCK", "MEDIUM_IN_STOCK")

    return {
        "available": available,
        "stock": total_stock,
        "probability": best_prob or "UNKNOWN",
        "restock_date": restock_date,
    }


# ── Telegram notification ─────────────────────────────────────────────────────


def send_notification(item_no: str, result: dict):
    token = CONFIG["telegram_token"]
    chat_id = CONFIG["telegram_chat_id"]
    if not token or not chat_id:
        console.print(
            "[yellow]⚠ Telegram not configured. Set IKEA_TELEGRAM_TOKEN and "
            "IKEA_TELEGRAM_CHAT_ID (or edit CONFIG in the script).[/yellow]"
        )
        return
    product_url = PRODUCT_URL.format(
        country=CONFIG["country"], lang=CONFIG["language"], item_no=item_no
    )
    msg = (
        f"🛒 *IKEA Chile — Product available!*\n\n"
        f"Article `{item_no}` is back in stock.\n"
        f"Stock: *{result['stock']}* units\n"
        f"Probability: *{result['probability']}*\n"
        f"[View on IKEA]({product_url})"
    )
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    try:
        httpx.post(
            url,
            json={"chat_id": chat_id, "text": msg, "parse_mode": "Markdown"},
            timeout=10,
        )
        console.print("[green]✓ Telegram notification sent.[/green]")
    except httpx.HTTPError as e:
        console.print(f"[red]Telegram error: {e}[/red]")


# ── State persistence (avoid duplicate notifications) ────────────────────────


def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            pass
    return {}


def save_state(state: dict):
    STATE_FILE.write_text(json.dumps(state, indent=2))


# ── Main loop ─────────────────────────────────────────────────────────────────


def run(item_nos: list[str], interval: int):
    console.print(
        Panel.fit(
            f"[bold blue]IKEA Chile Stock Monitor[/bold blue]\n"
            f"Watching [cyan]{len(item_nos)}[/cyan] product(s) • "
            f"Checking every [cyan]{interval}[/cyan] min • "
            f"Notifying via [cyan]Telegram[/cyan]",
            border_style="blue",
        )
    )

    state = load_state()

    console.print("[dim]Fetching product names…[/dim]")
    product_names = {
        item_no: fetch_product_name(item_no, CONFIG["country"], CONFIG["language"])
        for item_no in item_nos
    }
    for item_no, name in product_names.items():
        console.print(f"  [cyan]{item_no}[/cyan] → {name}")
    console.print()

    while True:
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        table = Table(title=f"Stock check — {now}", show_lines=True)
        table.add_column("Article", style="cyan", no_wrap=True)
        table.add_column("Description", style="dim")
        table.add_column("Available", justify="center")
        table.add_column("Stock", justify="right")
        table.add_column("Probability")
        table.add_column("Restock date")

        for item_no in item_nos:
            description = product_names.get(item_no, item_no)
            result = check_stock(item_no, CONFIG["country"])
            if result is None:
                table.add_row(item_no, description, "[red]Error[/red]", "-", "-", "-")
                continue

            was_available = state.get(item_no, {}).get("available", False)
            is_available = result["available"]

            avail_str = "[green]✓ YES[/green]" if is_available else "[red]✗ NO[/red]"
            table.add_row(
                item_no,
                description,
                avail_str,
                str(result["stock"]),
                result["probability"],
                result.get("restock_date") or "—",
            )

            # Notify only on transition: out-of-stock → in-stock
            if is_available and not was_available:
                console.print(
                    f"\n[bold green]🎉 {item_no} is now available! Sending Telegram message…[/bold green]"
                )
                send_notification(item_no, result)

            state[item_no] = {"available": is_available, "last_checked": now}

        console.print(table)
        save_state(state)

        console.print(
            f"[dim]Next check in {interval} minute(s)… (Ctrl+C to stop)[/dim]\n"
        )
        time.sleep(interval * 60)


# ── CLI ───────────────────────────────────────────────────────────────────────


def parse_args():
    parser = argparse.ArgumentParser(
        description="Monitor IKEA Chile product availability and get notified via Telegram.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "item_nos",
        nargs="+",
        metavar="ITEM_NO",
        help="One or more IKEA article numbers (e.g. 30623912)",
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=CONFIG["interval_minutes"],
        metavar="MINUTES",
        help=f"Check interval in minutes (default: {CONFIG['interval_minutes']})",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run a single check and exit (useful for cron jobs)",
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()

    if args.once:
        for item_no in args.item_nos:
            result = check_stock(item_no, CONFIG["country"])
            if result:
                status = "IN STOCK" if result["available"] else "OUT OF STOCK"
                rprint(
                    f"[bold]{item_no}[/bold]: {status} | stock={result['stock']} | prob={result['probability']}"
                )
                if result["available"]:
                    send_notification(item_no, result)
        sys.exit(0)

    try:
        run(args.item_nos, args.interval)
    except KeyboardInterrupt:
        console.print("\n[yellow]Monitor stopped.[/yellow]")
