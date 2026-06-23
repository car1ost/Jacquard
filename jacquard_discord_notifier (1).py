"""
Jacquard Products – Clearance / Overstock / Closeout Discord Notifier
======================================================================
Scrapes https://store.jacquardproducts.com/collections/closeouts every hour
and posts NEW deals to a Discord channel via a webhook.

SETUP
-----
1. Install dependencies:
       pip install requests beautifulsoup4 schedule

2. Create a Discord Webhook:
   - Open Discord → Server Settings → Integrations → Webhooks → New Webhook
   - Copy the webhook URL and paste it below (DISCORD_WEBHOOK_URL).

3. Run:
       python jacquard_discord_notifier.py

The script keeps track of products it has already announced (seen_products.json)
so you only get notified about NEW additions.
"""

import json
import os
import time
import logging
from datetime import datetime
from pathlib import Path

import requests
import schedule
from bs4 import BeautifulSoup

# ──────────────────────────────────────────────
# CONFIGURATION — edit these two lines
# ──────────────────────────────────────────────
DISCORD_WEBHOOK_URL = "https://discordapp.com/api/webhooks/1519038269336064084/kKeps34P4UQq8Kzc_eQ3IUVJWdgH2eLAfyJDC5GBO0j39jSKnPkTmqbc1qA5y55YmuYi"
CHECK_INTERVAL_MINUTES = 60          # how often to scrape
# ──────────────────────────────────────────────

STORE_URLS = [
    "https://store.jacquardproducts.com/collections/closeouts",
    # The store also tags items OVERSTOCK / CLOSEOUT sitewide; catch them too:
    "https://store.jacquardproducts.com/collections/all?sort_by=created-descending",
]

DEAL_KEYWORDS = ["clearance", "overstock", "closeout", "close out", "close-out"]

SEEN_FILE = Path("seen_products.json")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)


# ── Persistence ──────────────────────────────

def load_seen() -> set:
    if SEEN_FILE.exists():
        return set(json.loads(SEEN_FILE.read_text()))
    return set()


def save_seen(seen: set) -> None:
    SEEN_FILE.write_text(json.dumps(list(seen), indent=2))


# ── Scraping ─────────────────────────────────

def fetch_products_from_page(url: str) -> list[dict]:
    """Return a list of product dicts found on a collection page."""
    headers = {"User-Agent": "Mozilla/5.0 (compatible; JacquardDealBot/1.0)"}
    try:
        resp = requests.get(url, headers=headers, timeout=15)
        resp.raise_for_status()
    except requests.RequestException as e:
        log.warning(f"Could not fetch {url}: {e}")
        return []

    soup = BeautifulSoup(resp.text, "html.parser")
    products = []

    # Shopify stores typically render product cards with data-* attributes or
    # an <a> inside a product-card / grid__item element.
    # Try multiple common selectors so this works across theme variations.
    cards = (
        soup.select(".product-card")
        or soup.select(".grid__item")
        or soup.select("[data-product-id]")
        or soup.select("li.product")
    )

    if not cards:
        # Fallback: grab all <a> tags that look like product links
        cards = [
            a.parent for a in soup.select('a[href*="/products/"]')
            if a.get("href")
        ]

    for card in cards:
        # Title
        title_el = (
            card.select_one(".product-card__title")
            or card.select_one(".card__heading")
            or card.select_one("h2")
            or card.select_one("h3")
            or card.select_one(".product-title")
        )
        title = title_el.get_text(strip=True) if title_el else ""

        # Only keep items that mention deal keywords in the title
        if not any(kw in title.lower() for kw in DEAL_KEYWORDS):
            continue

        # Price
        price_el = (
            card.select_one(".price__sale")
            or card.select_one(".price")
            or card.select_one(".product-price")
        )
        price = price_el.get_text(strip=True) if price_el else "See site"

        # Link
        link_el = card.select_one('a[href*="/products/"]')
        link = (
            "https://store.jacquardproducts.com" + link_el["href"]
            if link_el and link_el.get("href", "").startswith("/")
            else (link_el["href"] if link_el else url)
        )

        # Unique ID = the product path segment
        product_id = link.rstrip("/").split("/products/")[-1]

        products.append(
            {
                "id": product_id,
                "title": title,
                "price": price,
                "url": link,
            }
        )

    return products


def scrape_all_deals() -> list[dict]:
    seen_ids: dict[str, dict] = {}
    for url in STORE_URLS:
        for p in fetch_products_from_page(url):
            seen_ids[p["id"]] = p   # deduplicate by product id
    return list(seen_ids.values())


# ── Discord ───────────────────────────────────

def build_embed(product: dict) -> dict:
    """Build a single Discord embed for one product."""
    title = product["title"]
    price = product["price"]
    url = product["url"]

    # Pick a colour based on deal type
    color = 0xFF6B6B   # red-ish default
    lower = title.lower()
    if "overstock" in lower:
        color = 0xF4A261   # orange
    elif "closeout" in lower or "close out" in lower:
        color = 0x2EC4B6   # teal
    elif "clearance" in lower:
        color = 0xE71D36   # bright red

    return {
        "title": title,
        "url": url,
        "color": color,
        "fields": [{"name": "Sale Price", "value": price, "inline": True}],
        "footer": {"text": "store.jacquardproducts.com"},
        "timestamp": datetime.utcnow().isoformat(),
    }


def post_to_discord(new_products: list[dict]) -> None:
    """Send up to 10 embeds per message (Discord limit)."""
    embeds = [build_embed(p) for p in new_products]

    # Discord allows max 10 embeds per message
    for i in range(0, len(embeds), 10):
        chunk = embeds[i : i + 10]
        payload = {
            "username": "Jacquard Deal Watcher 🎨",
            "avatar_url": "https://store.jacquardproducts.com/favicon.ico",
            "content": (
                f"🚨 **{len(new_products)} new deal(s)** found on "
                "Jacquard Products!"
                if i == 0
                else None
            ),
            "embeds": chunk,
        }
        resp = requests.post(DISCORD_WEBHOOK_URL, json=payload, timeout=10)
        if resp.status_code not in (200, 204):
            log.error(f"Discord error {resp.status_code}: {resp.text}")
        else:
            log.info(f"Posted {len(chunk)} embed(s) to Discord.")
        time.sleep(1)   # be polite to Discord rate limits


# ── Main check ───────────────────────────────

def check_for_deals() -> None:
    log.info("Checking Jacquard Products for deals…")
    seen = load_seen()
    deals = scrape_all_deals()

    new_deals = [p for p in deals if p["id"] not in seen]

    if not new_deals:
        log.info("No new deals found.")
        return

    log.info(f"Found {len(new_deals)} new deal(s): {[p['title'] for p in new_deals]}")

    if DISCORD_WEBHOOK_URL == "YOUR_DISCORD_WEBHOOK_URL_HERE":
        log.warning("Discord webhook not configured — printing deals instead:")
        for p in new_deals:
            print(f"  {p['title']}  {p['price']}  {p['url']}")
    else:
        post_to_discord(new_deals)

    seen.update(p["id"] for p in new_deals)
    save_seen(seen)


# ── Entry point ───────────────────────────────

if __name__ == "__main__":
    log.info(
        f"Jacquard Deal Watcher started — checking every {CHECK_INTERVAL_MINUTES} min."
    )

    # Run once immediately, then on schedule
    check_for_deals()

    schedule.every(CHECK_INTERVAL_MINUTES).minutes.do(check_for_deals)

    while True:
        schedule.run_pending()
        time.sleep(30)
