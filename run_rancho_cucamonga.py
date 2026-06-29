#!/usr/bin/env python3
"""
Phase 1 + Phase 2 extraction for Rancho Cucamonga, CA.

Usage:
    python run_rancho_cucamonga.py              # headless
    python run_rancho_cucamonga.py --visible    # visible browser
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import scraper
import phase2_extractor as p2

CITY      = "Rancho Cucamonga"
STATE     = "California"
CITY_SLUG = "rancho_cucamonga"


async def main():
    headless = "--visible" not in sys.argv

    # Register city so scraper builds the correct query
    scraper.CITY_STATES[CITY] = STATE
    scraper.QUERY_TYPES = ["assisted living", "memory care", "independent living"]

    # ── Phase 1 ───────────────────────────────────────────────────────────────
    print(f"\n=== Phase 1 — Scraping {CITY} (all 3 query types) ===\n")
    scraper.OUTPUT_DIR.mkdir(exist_ok=True)
    scraper.PHASE1_DIR.mkdir(exist_ok=True)
    seen_urls: set = set()

    from playwright.async_api import async_playwright
    async with async_playwright() as pw:
        await scraper.scrape_city(pw, CITY, headless, seen_urls)

    # ── Phase 2: Maps detail ──────────────────────────────────────────────────
    print(f"\n=== Phase 2 — Maps detail for {CITY} ===\n")
    await p2.run_maps(headless=headless, city_filter=CITY_SLUG)

    # ── Extraction: Website ───────────────────────────────────────────────────
    print(f"\n=== Extraction — Website scraping for {CITY} ===\n")
    await p2.run_website(web_headless=headless, city_filter=CITY_SLUG)


if __name__ == "__main__":
    asyncio.run(main())
