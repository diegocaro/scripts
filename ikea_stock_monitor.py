#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "httpx",
#   "python-dotenv",
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
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import httpx
from dotenv import load_dotenv
from rich import print as rprint
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

load_dotenv()

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
# Extracted from the IKEA Chile website's network calls. It's a public client ID used for availability checks.
# This is not a secret key, just an identifier for the IKEA website when calling the API.
# Go to a product page, open dev tools, and look for the availability API call to find the current client ID if needed.
INGKA_CLIENT_ID = "ef382663-a2a5-40d4-8afe-f0634821c0ed"

PRODUCT_URL = "https://www.ikea.com/{country}/{lang}/p/-{item_no}/"
STATE_FILE = Path.home() / ".ikea_stock_monitor_state.json"


@dataclass(frozen=True)
class StockResult:
    available: bool
    online_available: bool
    online_status: str
    store_stock: int
    store_restock_date: str | None
    store_restock_qty: int


# ── Product name lookup ───────────────────────────────────────────────────────
def _get_product_name_from_html(html: str) -> str | None:
    # Title format: "NAME Description, ... - IKEA Chile"
    match = re.search(r"<title>([^<]+)</title>", html)
    if match:
        title = match.group(1).strip()
        # Strip the trailing " - IKEA Chile" part
        title = re.sub(r"\s*-\s*IKEA.*$", "", title).strip()
        # Truncate at comma to keep it short
        short = title.split(",")[0].strip()
        return short or None
    return None


def fetch_product_name(item_no: str, country: str, language: str) -> str:
    """Get product name from the slug in the 301 redirect URL.
    Valid products redirect to /p/{slug}-{itemNo}/
    Invalid products redirect to /cat/productos-products/

    If product is valid
    """
    url = f"https://www.ikea.com/{country}/{language}/p/-{item_no}/"
    try:
        resp = httpx.get(
            url,
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=15,
            follow_redirects=False,
        )
        location = resp.headers.get("location", "")
        slug_match = re.search(r"/p/([a-z0-9-]+)-" + item_no + r"/", location)
        if not slug_match:
            return f"{item_no} (not found)"

        slug = slug_match.group(1).replace("-", " ").title()

        # Get from the HTML of the product page (some products don't have a proper slug)
        # if resp.status_code == 301:
        #     resp = httpx.get(
        #         location,
        #         headers={"User-Agent": "Mozilla/5.0"},
        #         timeout=15,
        #     )

        return _get_product_name_from_html(resp.text) or slug or item_no

    except Exception:
        return item_no


# ── Stock check ───────────────────────────────────────────────────────────────


def check_stock(item_no: str, country: str) -> StockResult | None:
    """
    Call IKEA's Ingka availability API and parse the response.

    The API returns multiple entries:
      - classUnitType "RU" (retail unit, e.g. "CL") = online/national availability
      - classUnitType "STO" = individual store availability
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

    online_available = False
    online_status = "OUT_OF_STOCK"
    store_stock = 0
    store_restock_date = None
    store_restock_qty = 0

    for entry in data.get("availabilities", []):
        unit_type = entry.get("classUnitKey", {}).get("classUnitType", "")
        buying = entry.get("buyingOption", {})

        if unit_type == "RU":
            # National / online availability
            hd = buying.get("homeDelivery", {})
            avail = hd.get("availability", {})
            msg = (
                avail.get("probability", {})
                .get("thisDay", {})
                .get("messageType", "OUT_OF_STOCK")
            )
            online_status = msg
            online_available = msg not in ("OUT_OF_STOCK",)

        elif unit_type == "STO":
            # Individual store — sum up quantities and collect earliest restock
            cc = buying.get("cashCarry", {})
            avail = cc.get("availability", {})
            store_stock += avail.get("quantity", 0) or 0
            for restock in avail.get("restocks", []):
                date = restock.get("earliestDate")
                qty = restock.get("quantity", 0) or 0
                if date and (store_restock_date is None or date < store_restock_date):
                    store_restock_date = date
                    store_restock_qty = qty

    available = online_available or store_stock > 0

    return StockResult(
        available=available,
        online_available=online_available,
        online_status=online_status,
        store_stock=store_stock,
        store_restock_date=store_restock_date,
        store_restock_qty=store_restock_qty,
    )


# ── Telegram notification ─────────────────────────────────────────────────────


def send_notification(item_no: str, result: StockResult):
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
    online = "✅ Available" if result.online_available else "❌ Out of stock"
    restock_line = (
        f"Restock expected: *{result.store_restock_date}* \\({result.store_restock_qty} units\\)\n"
        if result.store_restock_date
        else ""
    )
    msg = (
        f"🛒 *IKEA Chile — Product available\\!*\n\n"
        f"Article `{item_no}` is back in stock\\.\n"
        f"Online: *{online}*\n"
        f"Store stock: *{result.store_stock}* units\n"
        f"{restock_line}"
        f"[View on IKEA]({product_url})"
    )
    api_url = f"https://api.telegram.org/bot{token}/sendMessage"
    try:
        r = httpx.post(
            api_url,
            json={"chat_id": chat_id, "text": msg, "parse_mode": "MarkdownV2"},
            timeout=10,
        )
        r.raise_for_status()
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
        table.add_column("Online", justify="center")
        table.add_column("Store stock", justify="right")
        table.add_column("Restock date")
        table.add_column("Restock qty", justify="right")

        for item_no in item_nos:
            description = product_names.get(item_no, item_no)
            result = check_stock(item_no, CONFIG["country"])
            if result is None:
                table.add_row(item_no, description, "[red]Error[/red]", "-", "-", "-")
                continue

            was_available = state.get(item_no, {}).get("available", False)
            is_available = result.available

            online_str = (
                "[green]✓ YES[/green]" if result.online_available else "[red]✗ NO[/red]"
            )
            restock_str = result.store_restock_date or "—"
            restock_qty_str = (
                str(result.store_restock_qty) if result.store_restock_date else "—"
            )
            table.add_row(
                item_no,
                description,
                online_str,
                str(result.store_stock),
                restock_str,
                restock_qty_str,
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


def run_once(item_nos: list[str]):
    for item_no in item_nos:
        name = fetch_product_name(item_no, CONFIG["country"], CONFIG["language"])
        result = check_stock(item_no, CONFIG["country"])
        if result is None:
            rprint(f"[red]✗ {item_no}[/red]: could not fetch stock data")
            continue
        online = (
            "[green]✓ available[/green]"
            if result.online_available
            else "[red]✗ out of stock[/red]"
        )
        store = f"{result.store_stock} units in store"
        restock = (
            f"restock {result.store_restock_date} ({result.store_restock_qty} units)"
            if result.store_restock_date
            else "no restock info"
        )
        rprint(f"[bold]{name}[/bold] ([dim]{item_no}[/dim])")
        rprint(f"  Online:  {online}")
        rprint(f"  Store:   {store}")
        rprint(f"  Restock: {restock}")
        rprint("")
        if result.available:
            send_notification(item_no, result)


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
        type=lambda s: s.replace(".", ""),
        help="One or more IKEA article numbers (e.g. 30623912 or 306.239.12)",
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
        run_once(args.item_nos)
        sys.exit(0)

    try:
        run(args.item_nos, args.interval)
    except KeyboardInterrupt:
        console.print("\n[yellow]Monitor stopped.[/yellow]")
