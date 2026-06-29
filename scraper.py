#!/usr/bin/env python3
"""
Google Maps scraper — Phase 1.
Searches assisted living, memory care, and independent living for 92 US cities (all 50 states).
Per city: fresh incognito browser, scrolls all results, writes CSV immediately.
"""

import asyncio
import csv
import logging
import re
import sys
from pathlib import Path

from playwright.async_api import async_playwright

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

OUTPUT_DIR   = Path(__file__).parent / "outputs"
PHASE1_DIR   = OUTPUT_DIR / "phase1"

QUERY_TYPES = [
    "assisted living",
    "memory care",
    "independent living",
]
SCROLL_PAUSE_MS = 1800
MAX_STALE_ROUNDS = 4
SLOW_SCROLL_STEP = 300
SLOW_SCROLL_DELAY = 120

"""
DONE — 70 cities already scraped, not in active run:
"New York", "Los Angeles", "Chicago", "Houston", "Phoenix",
"Philadelphia", "San Antonio", "San Diego", "Dallas", "San Jose",
"Austin", "Jacksonville", "Fort Worth", "Columbus", "Charlotte",
"Indianapolis", "San Francisco", "Seattle", "Denver", "Nashville",
"Oklahoma City", "El Paso", "Washington", "Las Vegas", "Louisville",
"Memphis", "Portland", "Baltimore", "Milwaukee", "Albuquerque",
"Tucson", "Fresno", "Mesa", "Sacramento", "Kansas City",
"Atlanta", "Omaha", "Colorado Springs", "Raleigh", "Long Beach",
"Virginia Beach", "Minneapolis", "Tampa", "New Orleans", "Arlington",
"Bakersfield", "Honolulu", "Anaheim", "Aurora", "Corpus Christi",
"Riverside", "Lexington", "St. Louis", "Pittsburgh", "Stockton",
"Cincinnati", "St. Paul", "Toledo", "Greensboro", "Newark",
"Plano", "Henderson", "Orlando", "Lincoln", "Buffalo",
"Fort Wayne", "Jersey City", "Chandler", "Laredo", "Norfolk",
"""

US_CITIES = [
    # ── NEW (76) ───────────────────────────────────────────────────────────
    # 10 added for coverage gaps in 29 existing states
    "Miami", "Cleveland", "Tulsa", "Baton Rouge", "Madison",
    "Spokane", "Knoxville", "Augusta", "Salem", "Rockford",
    # 22 missing states — 3 cities each by population
    "Huntsville", "Birmingham", "Montgomery",       # Alabama
    "Anchorage", "Fairbanks", "Juneau",             # Alaska
    "Little Rock", "Fayetteville", "Fort Smith",    # Arkansas
    "Bridgeport", "Stamford", "New Haven",          # Connecticut
    "Wilmington", "Dover", "Middletown",            # Delaware
    "Boise", "Meridian", "Nampa",                   # Idaho
    "Des Moines", "Cedar Rapids", "Davenport",      # Iowa
    "Wichita", "Overland Park", "Olathe",           # Kansas
    "Lewiston", "Bangor", "South Portland",         # Maine
    "Boston", "Worcester", "Springfield",           # Massachusetts
    "Detroit", "Grand Rapids", "Warren",            # Michigan
    "Jackson", "Gulfport", "Southaven",             # Mississippi
    "Billings", "Missoula", "Great Falls",          # Montana
    "Manchester", "Nashua", "Concord",              # New Hampshire
    "Fargo", "Bismarck", "Grand Forks",             # North Dakota
    "Providence", "Cranston", "Warwick",            # Rhode Island
    "Columbia", "North Charleston", "Greenville",   # South Carolina
    "Sioux Falls", "Rapid City", "Aberdeen",        # South Dakota
    "Salt Lake City", "West Valley City", "Provo",  # Utah
    "Burlington", "South Burlington", "Rutland",    # Vermont
    "Charleston", "Huntington", "Morgantown",       # West Virginia
    "Cheyenne", "Casper", "Laramie",                # Wyoming
]

# State lookup — used to build precise search queries: "{city}, {state}, USA"
CITY_STATES = {
    "New York": "New York", "Los Angeles": "California", "Chicago": "Illinois",
    "Houston": "Texas", "Phoenix": "Arizona", "Philadelphia": "Pennsylvania",
    "San Antonio": "Texas", "San Diego": "California", "Dallas": "Texas",
    "San Jose": "California", "Austin": "Texas", "Jacksonville": "Florida",
    "Fort Worth": "Texas", "Columbus": "Ohio", "Charlotte": "North Carolina",
    "Indianapolis": "Indiana", "San Francisco": "California", "Seattle": "Washington",
    "Denver": "Colorado", "Nashville": "Tennessee", "Oklahoma City": "Oklahoma",
    "El Paso": "Texas", "Washington": "DC", "Las Vegas": "Nevada",
    "Louisville": "Kentucky", "Memphis": "Tennessee", "Portland": "Oregon",
    "Baltimore": "Maryland", "Milwaukee": "Wisconsin", "Albuquerque": "New Mexico",
    "Tucson": "Arizona", "Fresno": "California", "Mesa": "Arizona",
    "Sacramento": "California", "Kansas City": "Missouri", "Atlanta": "Georgia",
    "Omaha": "Nebraska", "Colorado Springs": "Colorado", "Raleigh": "North Carolina",
    "Long Beach": "California", "Virginia Beach": "Virginia", "Minneapolis": "Minnesota",
    "Tampa": "Florida", "New Orleans": "Louisiana", "Arlington": "Texas",
    "Bakersfield": "California", "Honolulu": "Hawaii", "Anaheim": "California",
    "Aurora": "Colorado", "Corpus Christi": "Texas", "Riverside": "California",
    "Lexington": "Kentucky", "St. Louis": "Missouri", "Pittsburgh": "Pennsylvania",
    "Stockton": "California", "Cincinnati": "Ohio", "St. Paul": "Minnesota",
    "Toledo": "Ohio", "Greensboro": "North Carolina", "Newark": "New Jersey",
    "Plano": "Texas", "Henderson": "Nevada", "Orlando": "Florida",
    "Lincoln": "Nebraska", "Buffalo": "New York", "Fort Wayne": "Indiana",
    "Jersey City": "New Jersey", "Chandler": "Arizona", "Laredo": "Texas",
    "Norfolk": "Virginia",
    # gap fills
    "Miami": "Florida", "Cleveland": "Ohio", "Tulsa": "Oklahoma",
    "Baton Rouge": "Louisiana", "Madison": "Wisconsin", "Spokane": "Washington",
    "Knoxville": "Tennessee", "Augusta": "Georgia", "Salem": "Oregon",
    "Rockford": "Illinois",
    # Alabama
    "Huntsville": "Alabama", "Birmingham": "Alabama", "Montgomery": "Alabama",
    # Alaska
    "Anchorage": "Alaska", "Fairbanks": "Alaska", "Juneau": "Alaska",
    # Arkansas
    "Little Rock": "Arkansas", "Fayetteville": "Arkansas", "Fort Smith": "Arkansas",
    # Connecticut
    "Bridgeport": "Connecticut", "Stamford": "Connecticut", "New Haven": "Connecticut",
    # Delaware
    "Wilmington": "Delaware", "Dover": "Delaware", "Middletown": "Delaware",
    # Idaho
    "Boise": "Idaho", "Meridian": "Idaho", "Nampa": "Idaho",
    # Iowa
    "Des Moines": "Iowa", "Cedar Rapids": "Iowa", "Davenport": "Iowa",
    # Kansas
    "Wichita": "Kansas", "Overland Park": "Kansas", "Olathe": "Kansas",
    # Maine
    "Lewiston": "Maine", "Bangor": "Maine", "South Portland": "Maine",
    # Massachusetts
    "Boston": "Massachusetts", "Worcester": "Massachusetts", "Springfield": "Massachusetts",
    # Michigan
    "Detroit": "Michigan", "Grand Rapids": "Michigan", "Warren": "Michigan",
    # Mississippi
    "Jackson": "Mississippi", "Gulfport": "Mississippi", "Southaven": "Mississippi",
    # Montana
    "Billings": "Montana", "Missoula": "Montana", "Great Falls": "Montana",
    # New Hampshire
    "Manchester": "New Hampshire", "Nashua": "New Hampshire", "Concord": "New Hampshire",
    # North Dakota
    "Fargo": "North Dakota", "Bismarck": "North Dakota", "Grand Forks": "North Dakota",
    # Rhode Island
    "Providence": "Rhode Island", "Cranston": "Rhode Island", "Warwick": "Rhode Island",
    # South Carolina
    "Columbia": "South Carolina", "North Charleston": "South Carolina", "Greenville": "South Carolina",
    # South Dakota
    "Sioux Falls": "South Dakota", "Rapid City": "South Dakota", "Aberdeen": "South Dakota",
    # Utah
    "Salt Lake City": "Utah", "West Valley City": "Utah", "Provo": "Utah",
    # Vermont
    "Burlington": "Vermont", "South Burlington": "Vermont", "Rutland": "Vermont",
    # West Virginia
    "Charleston": "West Virginia", "Huntington": "West Virginia", "Morgantown": "West Virginia",
    # Wyoming
    "Cheyenne": "Wyoming", "Casper": "Wyoming", "Laramie": "Wyoming",
}

BROWSER_ARGS = [
    "--incognito",
    "--no-sandbox",
    "--disable-gpu",
    "--disable-dev-shm-usage",
    "--disable-blink-features=AutomationControlled",
    "--window-size=1920,1080",
]
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


async def dismiss_consent(page):
    for label in ["Accept all", "Reject all", "I agree", "Accept"]:
        btn = page.locator(f'button:has-text("{label}")').first
        if await btn.count():
            await btn.click()
            await page.wait_for_timeout(1000)
            log.info("Dismissed consent dialog: %s", label)
            return


async def scroll_all_results(page):
    """
    1. Scroll feed to bottom, loading all lazy results.
       Stop when span.HlvSq (end-of-list) appears.
    2. Scroll back to top instantly.
    3. Slow sweep downward so every card renders in the viewport.
    4. Return to top for extraction.
    """
    feed = page.locator('div[role="feed"]')
    if not await feed.count():
        log.warning("Results feed not found.")
        return

    log.info("Phase 1 — scrolling down to load all results...")
    prev_count = 0
    stale = 0

    for attempt in range(300):
        await feed.evaluate("el => el.scrollTop = el.scrollHeight")
        await page.wait_for_timeout(SCROLL_PAUSE_MS)

        current_count = await page.locator("a.hfpxzc").count()
        log.info("  Round %d — %d results loaded", attempt + 1, current_count)

        # Exact selector the user identified for end-of-list
        if await page.locator("span.HlvSq").count():
            log.info("Detected span.HlvSq — end of list reached.")
            break

        if current_count == prev_count:
            stale += 1
            if stale >= MAX_STALE_ROUNDS:
                log.info("No new results after %d rounds — stopping scroll.", MAX_STALE_ROUNDS)
                break
        else:
            stale = 0
        prev_count = current_count

    total = await page.locator("a.hfpxzc").count()
    log.info("Total results: %d", total)

    # Phase 2 — back to top
    log.info("Phase 2 — scrolling back to top...")
    await feed.evaluate("el => el.scrollTop = 0")
    await page.wait_for_timeout(1500)

    # Phase 3 — slow downward sweep so all cards render
    log.info("Phase 3 — slow sweep down to render all cards...")
    scroll_height = await feed.evaluate("el => el.scrollHeight")
    pos = 0
    while pos <= scroll_height:
        await feed.evaluate(f"el => el.scrollTop = {pos}")
        await page.wait_for_timeout(SLOW_SCROLL_DELAY)
        pos += SLOW_SCROLL_STEP
        # Re-read scrollHeight in case new content loaded
        scroll_height = await feed.evaluate("el => el.scrollHeight")

    # Return to top for extraction
    await feed.evaluate("el => el.scrollTop = 0")
    await page.wait_for_timeout(800)
    log.info("Ready for extraction.")


def normalize_phone(raw: str) -> str:
    if not raw:
        return ""
    digits = re.sub(r"\D", "", raw)
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    if len(digits) == 10:
        return f"+1 ({digits[:3]}) {digits[3:6]} {digits[6:]}"
    return raw.strip()


def clean_aria(text: str) -> str:
    return re.sub(r"\s*·?\s*Visited link\s*$", "", text or "").strip()


def parse_rows(rows: list[str], rating: str) -> dict:
    """
    Parse card info rows into clean fields.
    Status collects ALL timing parts (e.g. "Open · Closes 6 pm").
    Phone is extracted separately from the same row.
    """
    category = address_lane1 = phone = ""
    status_parts = []

    for row in rows:
        parts = [p.strip() for p in row.split("·") if p.strip()]

        for part in parts:
            pl = part.lower()

            # Skip bare rating number or review count
            if part == rating or re.match(r"^\d+\.\d+$", part):
                continue
            if re.match(r"^\([\d,]+\)$", part):
                continue

            # Phone — matches +1 NNN-NNN-NNNN style
            if not phone and re.match(r"^\+?[\d][\d\s\-\(\)]{6,}$", part):
                phone = normalize_phone(part)
                continue

            # Timing / hours — collect ALL related parts to build full status
            if any(kw in pl for kw in ("open", "closed", "closes", "opens", "hours")):
                status_parts.append(part)
                continue

            # Category — no digits, reasonable length
            if not category and not any(c.isdigit() for c in part) and 3 < len(part) < 80:
                category = part
                continue

            # Address lane 1 — has street number OR street-type keyword
            if not address_lane1 and part != category:
                has_num = bool(re.search(r"\b\d{2,}\b", part))
                looks_like_street = bool(re.search(
                    r"\b(St|Ave|Rd|Dr|Blvd|Ln|Way|Pkwy|Ct|Pl|Hwy|Wy)\b", part, re.I
                ))
                if has_num or looks_like_street:
                    address_lane1 = part

    status = " · ".join(status_parts) if status_parts else ""
    return {"category": category, "address_lane1": address_lane1, "phone": phone, "status": status}


async def extract_card(card) -> dict:
    data = {
        "name": "",
        "rating": "",
        "reviews": "",
        "category": "",
        "address_lane1": "",
        "phone": "",
        "status": "",
        "website": "",
        "maps_url": "",
    }

    # Name + URL
    link = card.locator("a.hfpxzc").first
    if await link.count():
        data["name"] = clean_aria(await link.get_attribute("aria-label") or "")
        data["maps_url"] = await link.get_attribute("href") or ""

    # Rating from aria-label
    for sel in ['[aria-label*="stars"]', '[aria-label*="star"]']:
        el = card.locator(sel).first
        if await el.count():
            txt = await el.get_attribute("aria-label") or ""
            m = re.search(r"([\d.]+)\s+star", txt, re.I)
            if m:
                data["rating"] = m.group(1)
                break

    # Review count from aria-label
    for sel in ['[aria-label*="review"]']:
        el = card.locator(sel).first
        if await el.count():
            txt = await el.get_attribute("aria-label") or await el.inner_text()
            m = re.search(r"([\d,]+)", txt)
            if m:
                data["reviews"] = m.group(1).replace(",", "")
                break

    # Get each .W4Efsd row's full text via JS (avoids nested-span concatenation issues)
    row_texts = await card.evaluate("""
        el => {
            const rows = el.querySelectorAll('.W4Efsd > .W4Efsd');
            return Array.from(rows).map(r => r.innerText.trim()).filter(Boolean);
        }
    """)

    parsed = parse_rows(row_texts, data["rating"])
    data.update(parsed)

    # Website — the dedicated Website button on the card
    website_el = card.locator('a[data-value="Website"]').first
    if await website_el.count():
        data["website"] = await website_el.get_attribute("href") or ""

    return data


def city_slug(city: str) -> str:
    return city.lower().replace(" ", "_").replace(".", "")


def query_slug(qt: str) -> str:
    return qt.lower().replace(" ", "_")


async def scrape_query(p, city: str, query_type: str, headless: bool, seen_urls: set) -> list[dict]:
    """
    Scrape one (city × query_type) with a fresh incognito browser.
    Writes results to its own CSV: {city_slug}_phase1_{query_slug}.csv
    """
    state    = CITY_STATES.get(city, "")
    query    = f"{query_type} in {city}, {state}, USA" if state else f"{query_type} in {city}, USA"
    c_slug   = city_slug(city)
    q_slug   = query_slug(query_type)
    csv_path = PHASE1_DIR / f"{c_slug}_phase1_{q_slug}.csv"

    # Resume: skip if CSV already has data rows
    if csv_path.exists():
        existing = sum(1 for _ in csv_path.open(encoding="utf-8")) - 1
        if existing > 0:
            log.info("=== [%s | %s] Already done (%d rows) — skipping ===", city, query_type, existing)
            return []

    log.info("=== [%s | %s] Opening browser ===", city, query_type)

    csv_file   = open(csv_path, "w", newline="", encoding="utf-8")
    csv_writer = csv.DictWriter(csv_file, fieldnames=FIELDS)
    csv_writer.writeheader()
    csv_file.flush()

    browser = await p.chromium.launch(headless=headless, slow_mo=30, args=BROWSER_ARGS)
    context = await browser.new_context(viewport={"width": 1920, "height": 1080}, user_agent=UA)
    page    = await context.new_page()
    await page.add_init_script(
        "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
    )
    results = []

    try:
        url = "https://www.google.com/maps/search/" + query.replace(" ", "+")
        await page.goto(url, wait_until="load", timeout=60_000)
        await page.wait_for_selector('div[role="feed"]', timeout=30_000)
        await page.wait_for_timeout(2000)

        await dismiss_consent(page)
        await scroll_all_results(page)

        cards = page.locator("div.Nv2PK")
        total = await cards.count()
        log.info("  [%s | %s] %d cards found", city, query_type, total)

        for i in range(total):
            try:
                data = await extract_card(cards.nth(i))
                if data["name"] and data["maps_url"] not in seen_urls:
                    seen_urls.add(data["maps_url"])
                    results.append(data)
                    csv_writer.writerow({f: data.get(f, "") for f in FIELDS})
                    csv_file.flush()
                    log.info("  [%s | %s] %d — %s", city, query_type, i + 1, data["name"])
            except Exception as exc:
                log.warning("  [%s | %s] card %d skipped — %s", city, query_type, i + 1, exc)

    except Exception as e:
        log.error("  [%s | %s] failed — %s", city, query_type, e)
    finally:
        await browser.close()
        csv_file.close()
        log.info("=== [%s | %s] Browser closed. %d new results — %s ===",
                 city, query_type, len(results), csv_path.name)

    return results


async def scrape_city(p, city: str, headless: bool, seen_urls: set,
                      queue: asyncio.Queue = None) -> list[dict]:
    """
    Run all QUERY_TYPES for one city — each with its own browser and CSV.
    Puts (city_slug, query_slug) into queue after each query completes.
    """
    city_results = []
    for qt in QUERY_TYPES:
        qt_results = await scrape_query(p, city, qt, headless, seen_urls)
        city_results.extend(qt_results)
        if queue:
            await queue.put((city_slug(city), query_slug(qt)))
    log.info("City %s total: %d results", city, len(city_results))
    return city_results


async def scrape(headless: bool = True, queue: asyncio.Queue = None, concurrency: int = 2):
    OUTPUT_DIR.mkdir(exist_ok=True)
    PHASE1_DIR.mkdir(exist_ok=True)
    seen_urls = set()
    sem = asyncio.Semaphore(concurrency)

    async def run_city(p, city):
        async with sem:
            await scrape_city(p, city, headless, seen_urls, queue)

    async with async_playwright() as p:
        await asyncio.gather(*[run_city(p, city) for city in US_CITIES])

    if queue:
        await queue.put(None)   # sentinel — Phase 1 done
    log.info("Phase 1 complete.")


FIELDS = ["name", "rating", "reviews", "category", "address_lane1", "phone", "status", "website", "maps_url"]


async def main(headless: bool = True):
    await scrape(headless=headless)
    log.info("Done.")


if __name__ == "__main__":
    asyncio.run(main(headless="--headless" not in sys.argv))
