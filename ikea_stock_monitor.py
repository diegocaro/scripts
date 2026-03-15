#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = [
#   "httpx",
#   "python-dotenv",
#   "rich",
#   "tenacity",
# ]
# ///
"""
IKEA Chile Stock Monitor
========================
Monitors IKEA Chile product availability and notifies you via Telegram
when items are back in stock, or when they go low of stock.

Usage:
    chmod +x ikea_stock_monitor.py
    ./ikea_stock_monitor.py 10402841 40623913 ...

    # Load products from JSON file:
    ./ikea_stock_monitor.py --file products.json

    # Custom interval (minutes):
    ./ikea_stock_monitor.py --interval 60 10402841

    # Single check and exit (useful for cron):
    ./ikea_stock_monitor.py --once 10402841

    # Test Telegram integration:
    ./ikea_stock_monitor.py --test-telegram


Configuration (edit the CONFIG section below or use env vars):
    IKEA_TELEGRAM_TOKEN   Telegram bot token
    IKEA_TELEGRAM_CHAT_ID Telegram chat ID

State:
    Persists to ~/.ikea_stock_monitor_state.json to track availability
    Used to detect product transitions and avoid duplicate notifications

Changelog:
    2026-03-15
        • Added _escape_md helper to escape MarkdownV2 special characters in Telegram messages
        • Added store_status field to StockResult with colour-coded display in CLI and state persistence
        • Added --file option to load products from JSON
        • Added --test-telegram option to verify Telegram setup
        • Added --unit-tests flag with comprehensive test suite
        • Added _get_product_name_from_url function for URL slug extraction
        • Enhanced documentation with setup instructions and features
        • Improved function docstrings with detailed parameters and behavior

    2026-03-14
        • Initial release
        • Monitor IKEA Chile product availability via Telegram notifications
        • Support for online (home delivery) and store (cash & carry) availability
        • State persistence to avoid duplicate alerts
        • Configurable check intervals
        • Support for multiple products and custom article number formats
"""

import argparse
import json
import logging
import os
import re
import sys
import time
import unittest
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import httpx
from dotenv import load_dotenv
from rich import print as rprint
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential_jitter,
)

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
err_console = Console(stderr=True)
logger = logging.getLogger("ikea_stock_monitor")

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

_MARKDOWNV2_SPECIAL = re.compile(r"([_*\[\]()~`>#+\-=|{}.!\\])")


@dataclass(frozen=True)
class Product:
    item_no: str
    name: str

    @property
    def url(self) -> str:
        return PRODUCT_URL.format(
            country=CONFIG["country"], lang=CONFIG["language"], item_no=self.item_no
        )


@dataclass(frozen=True)
class StockResult:
    available: bool
    online_available: bool
    online_status: str
    store_stock: int
    store_status: str
    store_restock_date: str | None
    store_restock_qty: int

    @property
    def store_stock_formatted(self) -> str:
        color = (
            "green"
            if ("HIGH" in self.store_status or self.store_status == "IN_STOCK")
            else "yellow" if "LOW" in self.store_status else "red"
        )
        return f"[{color}]{self.store_stock}[/{color}]"


@dataclass(frozen=True)
class StockError:
    product: Product
    message: str


CheckResult = StockResult | StockError


def _log_request(request: httpx.Request):
    logger.debug("→ %s %s", request.method, request.url)


def _log_response(response: httpx.Response):
    logger.debug(
        "← %s %s (%s)",
        response.status_code,
        response.url,
        response.headers.get("content-type", ""),
    )


def _escape_md(text: str) -> str:
    """Escape special characters for Telegram MarkdownV2."""
    return _MARKDOWNV2_SPECIAL.sub(r"\\\1", text)


_http_client = httpx.Client(
    timeout=15,
    event_hooks={"request": [_log_request], "response": [_log_response]},
)


@retry(
    retry=retry_if_exception_type(httpx.HTTPError),
    stop=stop_after_attempt(3),
    wait=wait_exponential_jitter(initial=1, max=10),
    reraise=True,
)
def _fetch_url(
    url: str, *, headers: dict | None = None, follow_redirects: bool = True, **kwargs
) -> httpx.Response:
    resp = _http_client.get(
        url,
        headers=headers,
        follow_redirects=follow_redirects,
        **kwargs,
    )
    if not resp.is_redirect:
        resp.raise_for_status()
    return resp


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


def _get_product_name_from_url(url: str, item_no: str) -> str | None:
    # Looks for pattern: /p/{slug}-{itemNo}/

    slug_match = re.search(r"/p/([a-z0-9-]+)-" + re.escape(item_no) + r"/", url)
    if not slug_match:
        return None
    slug = slug_match.group(1).replace("-", " ").title()
    return slug


def fetch_product(item_no: str, country: str, language: str) -> Product:
    """Get product name from the slug in the 301 redirect URL.
    Valid products redirect to /p/{slug}-{itemNo}/
    Invalid products redirect to /cat/productos-products/
    """
    url = f"https://www.ikea.com/{country}/{language}/p/-{item_no}/"
    try:
        resp = _fetch_url(
            url,
            headers={"User-Agent": "Mozilla/5.0"},
            follow_redirects=False,
        )
    except httpx.HTTPError:
        logger.info("Failed to fetch product page for %s", item_no)
        return Product(item_no, item_no)

    location = resp.headers.get("location", "")
    slug = _get_product_name_from_url(location, item_no)

    if not slug:
        return Product(item_no, f"{item_no} (not found)")

    # Get from the HTML of the product page (some products don't have a proper slug)
    # if resp.status_code == 301:
    #     resp = httpx.get(
    #         location,
    #         headers={"User-Agent": "Mozilla/5.0"},
    #         timeout=15,
    #     )

    # product_name = _get_product_name_from_html(resp.text)
    return Product(item_no, slug or item_no)


def clean_item_no(s: str | int) -> str:
    return str(s).replace(".", "").strip()


# ── Stock check ───────────────────────────────────────────────────────────────


def parse_stock(data: dict) -> StockResult:
    """Pure function: parse Ingka availability API response into a StockResult."""
    entries = data.get("availabilities", [])

    ru_entries = [
        e for e in entries if e.get("classUnitKey", {}).get("classUnitType") == "RU"
    ]
    sto_entries = [
        e for e in entries if e.get("classUnitKey", {}).get("classUnitType") == "STO"
    ]

    # Online / national availability — take the first RU entry
    online_status = next(
        (
            e.get("buyingOption", {})
            .get("homeDelivery", {})
            .get("availability", {})
            .get("probability", {})
            .get("thisDay", {})
            .get("messageType", "OUT_OF_STOCK")
            for e in ru_entries
        ),
        "OUT_OF_STOCK",
    )
    online_available = online_status not in ("OUT_OF_STOCK",)

    # Store availability — sum quantities across all stores
    store_stock = sum(
        e.get("buyingOption", {})
        .get("cashCarry", {})
        .get("availability", {})
        .get("quantity", 0)
        or 0
        for e in sto_entries
    )

    # Store stock status message (from first STO entry that has one)
    store_status = next(
        (
            e.get("buyingOption", {})
            .get("cashCarry", {})
            .get("availability", {})
            .get("probability", {})
            .get("thisDay", {})
            .get("messageType", "OUT_OF_STOCK")
            for e in sto_entries
        ),
        "OUT_OF_STOCK",
    )

    # Earliest restock across all stores
    restocks = sorted(
        (
            (r["earliestDate"], r.get("quantity", 0) or 0)
            for e in sto_entries
            for r in e.get("buyingOption", {})
            .get("cashCarry", {})
            .get("availability", {})
            .get("restocks", [])
            if r.get("earliestDate")
        ),
        key=lambda t: t[0],
    )
    restock_date, restock_qty = restocks[0] if restocks else (None, 0)

    return StockResult(
        available=online_available or store_stock > 0,
        online_available=online_available,
        online_status=online_status,
        store_stock=store_stock,
        store_status=store_status,
        store_restock_date=restock_date,
        store_restock_qty=restock_qty,
    )


def check_stock(product: Product, country: str) -> CheckResult:
    """Fetch and parse availability from IKEA's Ingka API.

    Pure data flow: returns StockResult or StockError; all IO side-effects
    (notifications, console output) are left to the caller.
    """
    url = AVAILABILITY_URL.format(country=country, item_no=product.item_no)
    headers = {
        "Accept": "application/json;version=2",
        "X-Client-ID": INGKA_CLIENT_ID,
    }
    try:
        resp = _fetch_url(url, headers=headers)
    except httpx.HTTPStatusError as e:
        return StockError(product, f"HTTP error: {e}")
    except httpx.HTTPError as e:
        return StockError(product, f"Network error (after retries): {e}")

    try:
        data = resp.json()
        logger.debug("API response for %s: %s", product.item_no, resp.text)
    except Exception as e:
        return StockError(product, f"JSON parse error: {e}")

    result = parse_stock(data)
    logger.info(
        "%s: online=%s store=%d available=%s",
        product.item_no,
        result.online_status,
        result.store_stock,
        result.available,
    )
    return result


# ── Telegram notification ─────────────────────────────────────────────────────


def send_error_notification(product: Product, error: str):
    msg = (
        f"⚠️ *IKEA Chile — Error checking stock*\n\n"
        f"*{_escape_md(product.name)}* \\(`{product.item_no}`\\)\n"
        f"Error: {_escape_md(error)}"
    )
    try:
        _send_telegram(msg)
    except httpx.HTTPError as e:
        err_console.print(
            f"[red]Failed to send error notification via Telegram: {e}[/red]"
        )


def send_notification(product: Product, result: StockResult):
    token = CONFIG["telegram_token"]
    chat_id = CONFIG["telegram_chat_id"]
    if not token or not chat_id:
        console.print(
            "[yellow]⚠ Telegram not configured. Set IKEA_TELEGRAM_TOKEN and "
            "IKEA_TELEGRAM_CHAT_ID (or edit CONFIG in the script).[/yellow]"
        )
        return
    online = "✅ Available" if result.online_available else "❌ Out of stock"
    restock_line = (
        f"Restock expected: *{result.store_restock_date}* \\({result.store_restock_qty} units\\)\n"
        if result.store_restock_date
        else ""
    )
    msg = (
        f"🛒 *IKEA Chile — Product available\\!*\n\n"
        f"*{_escape_md(product.name)}* \\(`{product.item_no}`\\) is back in stock\\.\n"
        f"Online: *{online}*\n"
        f"Store stock: *{result.store_stock}* units\n"
        f"{restock_line}"
        f"[View on IKEA]({product.url})"
    )
    try:
        _send_telegram(msg)
        console.print("[green]✓ Telegram notification sent.[/green]")
    except httpx.HTTPError as e:
        err_console.print(f"[red]Failed to send Telegram notification: {e}[/red]")


@retry(
    retry=retry_if_exception_type(httpx.HTTPError),
    stop=stop_after_attempt(3),
    wait=wait_exponential_jitter(initial=1, max=10),
    reraise=True,
)
def _send_telegram(text: str):
    token = CONFIG["telegram_token"]
    chat_id = CONFIG["telegram_chat_id"]
    if not token or not chat_id:
        return
    api_url = f"https://api.telegram.org/bot{token}/sendMessage"

    r = httpx.post(
        api_url,
        json={"chat_id": chat_id, "text": text, "parse_mode": "MarkdownV2"},
        timeout=10,
    )
    r.raise_for_status()


# ── Pure state helpers ───────────────────────────────────────────────────────


def should_notify(old_entry: dict | None, result: StockResult) -> bool:
    """Pure predicate: True when a product transitions from unavailable to available."""
    return result.available and not (old_entry or {}).get("available", False)


def make_state_entry(result: StockResult, now: str) -> dict:
    """Pure factory: build the state dict entry for a product."""
    return {
        "available": result.available,
        "store_status": result.store_status,
        "last_checked": now,
    }


# ── State persistence (avoid duplicate notifications) ────────────────────────


def now_iso() -> str:
    return datetime.now().astimezone().isoformat()


def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            state = json.loads(STATE_FILE.read_text())
            logger.info("Loaded state with %d items", len(state))
            return state
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

    console.print("[dim]Fetching product info…[/dim]")
    products = [
        fetch_product(item_no, CONFIG["country"], CONFIG["language"])
        for item_no in item_nos
    ]
    for p in products:
        console.print(f"  [cyan]{p.item_no}[/cyan] → {p.name}")
    console.print()

    while True:
        now = now_iso()
        table = Table(title=f"Stock check — {now}", show_lines=True)
        table.add_column("Article", style="cyan", no_wrap=True)
        table.add_column("Description", style="dim")
        table.add_column("Online", justify="center")
        table.add_column("Store stock", justify="right")
        table.add_column("Restock date")
        table.add_column("Restock qty", justify="right")

        for product in products:
            result = check_stock(product, CONFIG["country"])

            match result:
                case StockError(message=msg):
                    err_console.print(f"[red]{msg} for {product.item_no}[/red]")
                    send_error_notification(product, msg)
                    table.add_row(
                        product.item_no,
                        product.name,
                        "[red]Error[/red]",
                        "-",
                        "-",
                        "-",
                    )
                    continue
                case StockResult():
                    pass

            online_str = (
                "[green]✓ YES[/green]" if result.online_available else "[red]✗ NO[/red]"
            )
            restock_str = result.store_restock_date or "—"
            restock_qty_str = (
                str(result.store_restock_qty) if result.store_restock_date else "—"
            )
            table.add_row(
                product.item_no,
                product.name,
                online_str,
                result.store_stock_formatted,
                restock_str,
                restock_qty_str,
            )

            # Notify only on transition: out-of-stock → in-stock
            if should_notify(state.get(product.item_no), result):
                logger.info(
                    "Transition for %s: unavailable → available", product.item_no
                )
                console.print(
                    f"\n[bold green]🎉 {product.item_no} is now available! Sending Telegram message…[/bold green]"
                )
                send_notification(product, result)

            state[product.item_no] = make_state_entry(result, now)

        console.print(table)
        save_state(state)

        console.print(
            f"[dim]Next check in {interval} minute(s)… (Ctrl+C to stop)[/dim]\n"
        )
        time.sleep(interval * 60)


def run_once(item_nos: list[str]):
    state = load_state()
    now = now_iso()

    for item_no in item_nos:
        product = fetch_product(item_no, CONFIG["country"], CONFIG["language"])
        result = check_stock(product, CONFIG["country"])

        match result:
            case StockError(message=msg):
                err_console.print(f"[red]{msg} for {product.item_no}[/red]")
                send_error_notification(product, msg)
                rprint(f"[red]✗ {product.item_no}[/red]: {msg}")
                continue
            case StockResult():
                pass

        online = (
            "[green]✓ available[/green]"
            if result.online_available
            else "[red]✗ out of stock[/red]"
        )
        store = f"{result.store_stock_formatted} units in store"
        restock = (
            f"restock {result.store_restock_date} ({result.store_restock_qty} units)"
            if result.store_restock_date
            else "no restock info"
        )
        rprint(f"[bold]{product.name}[/bold] ([dim]{product.item_no}[/dim])")
        rprint(f"  Online:  {online}")
        rprint(f"  Store:   {store}")
        rprint(f"  Restock: {restock}")
        rprint("")

        if should_notify(state.get(product.item_no), result):
            send_notification(product, result)

        state[product.item_no] = make_state_entry(result, now)

    save_state(state)


def test_telegram():
    token = CONFIG["telegram_token"]
    chat_id = CONFIG["telegram_chat_id"]
    if not token or not chat_id:
        console.print(
            "[red]Telegram not configured. Set IKEA_TELEGRAM_TOKEN and "
            "IKEA_TELEGRAM_CHAT_ID.[/red]"
        )
        sys.exit(1)
    msg = "✅ IKEA Stock Monitor — Telegram integration is working\\!"
    try:
        _send_telegram(msg)
        console.print("[green]✓ Test message sent successfully.[/green]")
    except httpx.HTTPError as e:
        err_console.print(f"[red]✗ Failed to send test message: {e}[/red]")
        sys.exit(1)


# ── Unit Tests ───────────────────────────────────────────────────────────────────────


class TestCleanItemNo(unittest.TestCase):
    """Tests for item number formatting."""

    def test_formatted_item_no(self):
        self.assertEqual(clean_item_no("104.028.41"), "10402841")

    def test_integer_item_no(self):
        self.assertEqual(clean_item_no(10402841), "10402841")

    def test_regular_item_no(self):
        self.assertEqual(clean_item_no("10402841"), "10402841")


class TestProductNameExtraction(unittest.TestCase):
    """Tests for product name extraction from HTML and URL."""

    def test_extract_product_name_from_html(self):
        html = "<html><head><title>BILLSTA Espejo de pared, plateado, 26x26 cm - IKEA Chile</title></head></html>"
        result = _get_product_name_from_html(html)
        self.assertEqual(result, "BILLSTA Espejo de pared")

    def test_no_title_tag(self):
        html = "<html><head></head></html>"
        result = _get_product_name_from_html(html)
        self.assertIsNone(result)

    def test_extract_product_name_from_url(self):
        url = "/p/billsta-espejo-de-pared-10402841/"
        result = _get_product_name_from_url(url, "10402841")
        self.assertEqual(result, "Billsta Espejo De Pared")

    def test_extract_product_name_from_url_with_dots_in_item_no(self):
        url = "/p/billsta-espejo-de-pared-104.028.41/"
        result = _get_product_name_from_url(url, "104.028.41")
        self.assertEqual(result, "Billsta Espejo De Pared")

    def test_product_name_from_url_not_found(self):
        url = "/cat/productos-products/"
        result = _get_product_name_from_url(url, "10402841")
        self.assertIsNone(result)


class TestStockParsing(unittest.TestCase):
    """Tests for stock data parsing."""

    def test_parse_stock_in_stock(self):
        data = {
            "availabilities": [
                {
                    "classUnitKey": {"classUnitType": "RU"},
                    "buyingOption": {
                        "homeDelivery": {
                            "availability": {
                                "probability": {"thisDay": {"messageType": "IN_STOCK"}}
                            }
                        }
                    },
                },
                {
                    "classUnitKey": {"classUnitType": "STO"},
                    "buyingOption": {
                        "cashCarry": {"availability": {"quantity": 5, "restocks": []}}
                    },
                },
            ]
        }
        result = parse_stock(data)
        self.assertTrue(result.online_available)
        self.assertEqual(result.store_stock, 5)
        self.assertTrue(result.available)

    def test_parse_stock_out_of_stock(self):
        data = {
            "availabilities": [
                {
                    "classUnitKey": {"classUnitType": "RU"},
                    "buyingOption": {
                        "homeDelivery": {
                            "availability": {
                                "probability": {
                                    "thisDay": {"messageType": "OUT_OF_STOCK"}
                                }
                            }
                        }
                    },
                },
                {
                    "classUnitKey": {"classUnitType": "STO"},
                    "buyingOption": {
                        "cashCarry": {"availability": {"quantity": 0, "restocks": []}}
                    },
                },
            ]
        }
        result = parse_stock(data)
        self.assertFalse(result.online_available)
        self.assertEqual(result.store_stock, 0)
        self.assertFalse(result.available)

    def test_parse_stock_with_restock_info(self):
        data = {
            "availabilities": [
                {
                    "classUnitKey": {"classUnitType": "RU"},
                    "buyingOption": {
                        "homeDelivery": {
                            "availability": {
                                "probability": {
                                    "thisDay": {"messageType": "OUT_OF_STOCK"}
                                }
                            }
                        }
                    },
                },
                {
                    "classUnitKey": {"classUnitType": "STO"},
                    "buyingOption": {
                        "cashCarry": {
                            "availability": {
                                "quantity": 0,
                                "restocks": [
                                    {"earliestDate": "2026-03-20", "quantity": 10}
                                ],
                            }
                        }
                    },
                },
            ]
        }
        result = parse_stock(data)
        self.assertEqual(result.store_restock_date, "2026-03-20")
        self.assertEqual(result.store_restock_qty, 10)


def run_tests() -> unittest.TestResult:
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    suite.addTests(loader.loadTestsFromTestCase(TestCleanItemNo))
    suite.addTests(loader.loadTestsFromTestCase(TestProductNameExtraction))
    suite.addTests(loader.loadTestsFromTestCase(TestStockParsing))
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    return result


# ── CLI ───────────────────────────────────────────────────────────────────────


def parse_args():
    parser = argparse.ArgumentParser(
        description="Monitor IKEA Chile product availability and get notified via Telegram.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "item_nos",
        nargs="*",
        metavar="ITEM_NO",
        type=clean_item_no,
        help="One or more IKEA article numbers (e.g. 10402841 or 104.028.41)",
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
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Show extra info (stock parse results, state transitions)",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Enable debug logging (implies --verbose, shows HTTP requests/responses)",
    )
    parser.add_argument(
        "--test-telegram",
        action="store_true",
        help="Send a test message to Telegram and exit",
    )
    parser.add_argument(
        "-f",
        "--file",
        type=Path,
        metavar="FILE",
        help='JSON file with a list of article numbers (e.g. ["40623913","104.028.41"])',
    )
    parser.add_argument(
        "--unit-tests",
        action="store_true",
        help="Run unit tests for core functionality and exit",
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    if args.debug:
        level = logging.DEBUG
    elif args.verbose:
        level = logging.INFO
    else:
        level = logging.WARNING
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )

    if args.unit_tests:
        result = run_tests()
        sys.exit(0 if result.wasSuccessful() else 1)

    if args.test_telegram:
        test_telegram()
        sys.exit(0)

    item_nos = list(args.item_nos or [])
    if args.file:
        try:
            data = json.loads(args.file.read_text())
            item_nos.extend(clean_item_no(n) for n in data)
        except (json.JSONDecodeError, OSError) as e:
            err_console.print(f"[red]Error reading {args.file}: {e}[/red]")
            sys.exit(1)

    if not item_nos:
        err_console.print(
            "[red]Error: provide ITEM_NOs as arguments or via --file.[/red]"
        )
        sys.exit(1)

    if args.once:
        run_once(item_nos)
        sys.exit(0)

    try:
        run(item_nos, args.interval)
    except KeyboardInterrupt:
        console.print("\n[yellow]Monitor stopped.[/yellow]")
