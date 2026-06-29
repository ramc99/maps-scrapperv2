#!/usr/bin/env python3
"""
Phase 2 Extractor — Maps detail + website extraction
Reads Phase 1 CSVs → writes Phase 2 (maps) and Extraction (website) CSVs.

Usage:
    python phase2_extractor.py                    # all cities, both stages
    python phase2_extractor.py --maps             # maps detail only
    python phase2_extractor.py --website          # website extraction only
    python phase2_extractor.py Houston            # single city
    python phase2_extractor.py Houston 5          # first 5 rows (testing)
    python phase2_extractor.py --workers=3
    python phase2_extractor.py --visible
"""

import asyncio
import csv
import json
import logging
import re
import sys
import time as _time
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from playwright.async_api import async_playwright
from selenium import webdriver
from selenium.webdriver.chrome.options import Options as ChromeOptions

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

# ── Paths ──────────────────────────────────────────────────────────────────────
OUTPUT_DIR     = Path(__file__).parent / "outputs"
PHASE1_DIR     = OUTPUT_DIR / "phase1"
PHASE2_DIR     = OUTPUT_DIR / "phase2"
EXTRACTION_DIR = OUTPUT_DIR / "extraction"

# ── Config ─────────────────────────────────────────────────────────────────────
QUERY_SLUGS   = ["assisted_living", "memory_care", "independent_living"]
MAPS_WORKERS  = 2
WEB_SEM_SIZE  = 3
DELAY_MS      = 1000
MAX_PHOTOS    = 20
RESTART_EVERY = 10
WEB_TIMEOUT   = 15
MAX_WEB_PAGES = 7
MIN_CONTENT_LEN = 2000   # chars before Selenium fallback is triggered

BROWSER_ARGS = [
    "--incognito", "--no-sandbox", "--disable-gpu",
    "--disable-dev-shm-usage", "--disable-blink-features=AutomationControlled",
    "--window-size=1920,1080",
]
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
WEB_HEADERS = {"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"}

SUB_PAGE_KEYWORDS = [
    "about", "contact", "team", "staff", "services", "amenities",
    "care", "faq", "mission", "vision", "residents", "life", "community",
    "activities", "dining", "pricing", "floor-plan",
]

_WEB_SEM: asyncio.Semaphore = None


# ── CSV output fields ──────────────────────────────────────────────────────────
PHASE2_FIELDS = [
    "name", "rating", "reviews", "category",
    "address_lane1", "phone", "maps_status", "website", "maps_url",
    "maps_address", "maps_plus_code", "maps_hours", "maps_about",
    "lat", "lng", "maps_photos",
    "street1", "addr_city", "addr_state", "postal_code",
]

EXTRACTION_FIELDS = PHASE2_FIELDS + [
    "web_facility_name", "web_email", "web_fax", "web_extra_phones", "web_social",
    "web_about", "web_mission", "web_vision", "web_description",
    "web_amenities", "web_special_services", "web_faqs",
    "web_team", "web_testimonials", "web_seo_keywords",
    "web_pages_scraped", "web_extraction_status",
]

FIELDS = PHASE2_FIELDS   # backward-compat alias


# ══════════════════════════════════════════════════════════════════════════════
# SHARED HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def normalize_phone(raw: str) -> str:
    if not raw:
        return ""
    digits = re.sub(r"\D", "", raw)
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    if len(digits) == 10:
        return f"+1 ({digits[:3]}) {digits[3:6]} {digits[6:]}"
    return raw.strip()


def parse_address(full_addr: str) -> dict:
    parts = [p.strip() for p in full_addr.split(",")]
    street1 = parts[0] if parts else ""
    addr_city = addr_state = postal_code = ""
    if len(parts) >= 2:
        last = parts[-1].strip()
        m = re.match(r"^([A-Z]{2})\s+(\d{5}(?:-\d{4})?)$", last)
        if m:
            addr_state  = m.group(1)
            postal_code = m.group(2)
            addr_city   = parts[-2].strip() if len(parts) >= 3 else ""
        else:
            zm = re.search(r"\b(\d{5}(?:-\d{4})?)\b", full_addr)
            if zm:
                postal_code = zm.group(1)
            sm = re.search(r"\b([A-Z]{2})\b", last)
            if sm:
                addr_state = sm.group(1)
            addr_city = parts[1].strip() if len(parts) >= 2 else ""
    return {"street1": street1, "addr_city": addr_city,
            "addr_state": addr_state, "postal_code": postal_code}


def _clean(text, limit=800):
    return re.sub(r"\s+", " ", text or "").strip()[:limit]


# ══════════════════════════════════════════════════════════════════════════════
# MAPS DETAIL EXTRACTION (Playwright)
# ══════════════════════════════════════════════════════════════════════════════

DAYS_FULL  = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
DAYS_SHORT = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


async def expand_hours(page):
    for sel in [
        'button[aria-label*="hours" i][aria-expanded="false"]',
        'button[aria-label*="hour" i][aria-expanded="false"]',
        '[jsaction*="openhours"]',
        '.t39EBf',
        'div[aria-label*="hour" i]',
        '[data-item-id*="oh"] button',
    ]:
        try:
            el = page.locator(sel).first
            if await el.count():
                await el.click()
                await page.wait_for_timeout(1000)
                return
        except Exception:
            continue


async def extract_hours(page) -> str:
    await expand_hours(page)
    entries = []

    # Strategy 1 — table rows
    rows = None
    for sel in [
        "table.eK4R0e tr",
        "tr.y0skZc",
        ".t39EBf tr",
        "[data-hide-tooltip-on-mouse-move] tr",
        ".OqCZI tr",
        "table tr",
    ]:
        loc = page.locator(sel)
        try:
            if await loc.count() > 0:
                rows = loc
                break
        except Exception:
            continue

    if rows:
        for i in range(await rows.count()):
            try:
                raw = await rows.nth(i).inner_text()
            except Exception:
                continue
            text = re.sub(r"\s+", " ", raw).strip()
            for full, short in zip(DAYS_FULL, DAYS_SHORT):
                if text.lower().startswith(full.lower()):
                    time_part = text[len(full):].strip().lstrip(".,: \t")
                    time_part = re.split(r"\s*\(", time_part)[0].strip()
                    if time_part:
                        entries.append(f"{full}: {time_part}")
                    break
                if text.lower().startswith(short.lower() + " ") or text.lower() == short.lower():
                    time_part = text[len(short):].strip().lstrip(".,: \t")
                    time_part = re.split(r"\s*\(", time_part)[0].strip()
                    if time_part:
                        entries.append(f"{full}: {time_part}")
                    break
        if entries:
            return json.dumps(entries, ensure_ascii=False)

    # Strategy 2 — aria-label encoding
    for sel in [
        '[aria-label*="Sunday" i]',
        '[aria-label*="Monday" i]',
        'div[jsaction*="openhours"]',
    ]:
        try:
            el = page.locator(sel).first
            if await el.count():
                aria = await el.get_attribute("aria-label") or ""
                for full in DAYS_FULL:
                    m = re.search(rf"{full}[,:\s]+([^\n;\.]+)", aria, re.I)
                    if m:
                        time_part = re.split(r"\s*\(", m.group(1))[0].strip()
                        if time_part:
                            entries.append(f"{full}: {time_part}")
                if entries:
                    return json.dumps(entries, ensure_ascii=False)
        except Exception:
            continue

    # Strategy 3 — body text regex
    if not entries:
        try:
            page_text = await page.locator("body").inner_text()
            for full in DAYS_FULL:
                m = re.search(
                    rf"\b{full}\b[^a-zA-Z\n]{{0,5}}(\d{{1,2}}(?::\d{{2}})?\s*(?:am|pm)[^\n;,]{{0,30}})",
                    page_text, re.I
                )
                if m:
                    entries.append(f"{full}: {m.group(1).strip()}")
        except Exception:
            pass

    return json.dumps(entries, ensure_ascii=False)


async def extract_maps_about(page) -> str:
    entries = []
    for sel in [
        ".LTs0Rc [class*='fontBodyMedium']",
        "[data-item-id*='about'] .fontBodyMedium",
        ".iP2t7d .fontBodyMedium",
        "div[aria-label*='About'] .fontBodyMedium",
    ]:
        try:
            loc = page.locator(sel)
            if await loc.count():
                for i in range(min(await loc.count(), 20)):
                    txt = re.sub(r"\s+", " ", await loc.nth(i).inner_text()).strip()
                    if txt and txt not in entries:
                        entries.append(txt)
                if entries:
                    break
        except Exception:
            continue
    return json.dumps(entries, ensure_ascii=False)


async def extract_photos(page) -> str:
    urls = []
    for sel in [
        "button[jsaction*='pane.heroHeaderImage'] img",
        "img[src*='googleusercontent']",
        ".gallery-cell img",
        "[data-photo-index] img",
    ]:
        try:
            imgs = page.locator(sel)
            cnt = await imgs.count()
            if cnt:
                for i in range(min(cnt, MAX_PHOTOS)):
                    src = await imgs.nth(i).get_attribute("src") or ""
                    if src and "googleusercontent" in src and src not in urls:
                        urls.append(src)
                if urls:
                    break
        except Exception:
            continue
    return ", ".join(urls)


async def extract_coords(page) -> tuple:
    try:
        m = re.search(r"@(-?\d+\.\d+),(-?\d+\.\d+)", page.url)
        if m:
            return m.group(1), m.group(2)
    except Exception:
        pass
    return "", ""


async def extract_full_address(page) -> str:
    for sel in [
        'button[data-item-id="address"]',
        '[data-tooltip="Copy address"]',
        '.rogA2c .Io6YTe',
        'button[aria-label*="Address"]',
    ]:
        try:
            el = page.locator(sel).first
            if await el.count():
                txt = re.sub(r"\s+", " ", await el.inner_text()).strip()
                if txt:
                    return txt
        except Exception:
            continue
    return ""


async def extract_plus_code(page) -> str:
    for sel in [
        'button[data-item-id="oloc"]',
        '[aria-label*="Plus code"]',
    ]:
        try:
            el = page.locator(sel).first
            if await el.count():
                return (await el.inner_text()).strip()
        except Exception:
            continue
    return ""


async def scrape_maps_detail(page, maps_url: str, name: str) -> dict:
    data = {f: "" for f in FIELDS}
    data["name"] = name
    if not maps_url:
        return data
    try:
        await page.goto(maps_url, wait_until="domcontentloaded", timeout=30_000)
        await page.wait_for_timeout(2500)
        data["maps_address"]   = await extract_full_address(page)
        data["maps_plus_code"] = await extract_plus_code(page)
        data["maps_hours"]     = await extract_hours(page)
        data["maps_about"]     = await extract_maps_about(page)
        data["maps_photos"]    = await extract_photos(page)
        lat, lng = await extract_coords(page)
        data["lat"] = lat
        data["lng"]  = lng
        if data["maps_address"]:
            data.update(parse_address(data["maps_address"]))
    except Exception as e:
        log.warning("Maps detail failed — %s: %s", name, e)
    return data


# ══════════════════════════════════════════════════════════════════════════════
# WEBSITE EXTRACTION — parsing helpers
# ══════════════════════════════════════════════════════════════════════════════

_EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")
_PHONE_RE = re.compile(r"\+?1?[\s.\-]?\(?\d{3}\)?[\s.\-]?\d{3}[\s.\-]?\d{4}")
_SOCIAL_DOMAINS = (
    "facebook.com", "twitter.com", "x.com", "instagram.com",
    "linkedin.com", "youtube.com", "tiktok.com",
)
_SPAM_EMAILS = {"example@example.com", "email@email.com", "noreply@example.com"}

# Noise patterns that indicate a section is NOT mission/vision content
_SECTION_NOISE = re.compile(
    r"\b(upcoming|event|dance|celebrate|register|sign.?up|join us|rsvp|calendar|schedule|"
    r"entertainment|activities|program|class|workshop)\b",
    re.I
)

# UI text fragments that pollute FAQ questions from accordion components
_UI_NOISE = re.compile(
    r"\s*(open accordion item|close accordion item|expand|collapse|show more|show less|read more|"
    r"click to expand|click to collapse|toggle|view more|view less)\s*",
    re.I
)

# Testimonial quality: must look like human text (has lowercase + punctuation or is long)
_FIRST_PERSON = re.compile(r"\b(I |I'|my |we |our |I've|I'm|I'd|I'll)\b", re.I)


def _get_sub_page_urls(soup, base_url: str) -> list:
    base_domain = urlparse(base_url).netloc
    found, seen = [], {base_url.rstrip("/")}
    for a in soup.find_all("a", href=True):
        try:
            full = urljoin(base_url, a["href"]).split("?")[0].split("#")[0].rstrip("/")
        except Exception:
            continue
        if urlparse(full).netloc != base_domain:
            continue
        path = urlparse(full).path.lower()
        if any(kw in path for kw in SUB_PAGE_KEYWORDS) and full not in seen:
            seen.add(full)
            found.append(full)
        if len(found) >= MAX_WEB_PAGES:
            break
    return found


def _parse_soup(soup, existing_phone="") -> dict:
    page_text = soup.get_text(" ", strip=True)
    return {
        "web_facility_name":    _extract_facility_name(soup),
        "web_email":            "; ".join(_extract_emails(soup, page_text)),
        "web_fax":              _extract_fax(soup, page_text),
        "web_extra_phones":     "; ".join(_extract_phones(page_text, existing_phone)),
        "web_social":           "; ".join(_extract_social(soup)),
        "web_about":            _extract_about(soup, page_text),
        "web_mission":          _find_section(soup, "mission"),
        "web_vision":           _find_section(soup, "vision"),
        "web_description":      _extract_description(soup),
        "web_amenities":        json.dumps(_extract_amenities(soup), ensure_ascii=False),
        "web_special_services": json.dumps(_extract_special_services(soup), ensure_ascii=False),
        "web_faqs":             json.dumps(_extract_faqs(soup), ensure_ascii=False),
        "web_team":             json.dumps(_extract_team(soup), ensure_ascii=False),
        "web_testimonials":     json.dumps(_extract_testimonials(soup), ensure_ascii=False),
        "web_seo_keywords":     json.dumps(_extract_seo_keywords(soup, page_text), ensure_ascii=False),
        "_page_text_len":       len(page_text),
    }


def _merge_results(results: list) -> dict:
    merged = {
        "web_facility_name": "", "web_email": "", "web_fax": "",
        "web_extra_phones": "", "web_social": "",
        "web_about": "", "web_mission": "", "web_vision": "", "web_description": "",
        "web_amenities": "[]", "web_special_services": "[]",
        "web_faqs": "[]", "web_team": "[]",
        "web_testimonials": "[]", "web_seo_keywords": "[]",
    }
    all_emails, all_phones, all_social = set(), set(), set()
    all_amenities, all_services, all_faqs = [], [], []
    all_team, all_testimonials, all_keywords = [], [], []

    for r in results:
        for f in ("web_facility_name", "web_fax", "web_about",
                  "web_mission", "web_vision", "web_description"):
            if not merged[f] and r.get(f):
                merged[f] = r[f]
        all_emails.update(e for e in r.get("web_email", "").split("; ") if e)
        all_phones.update(p for p in r.get("web_extra_phones", "").split("; ") if p)
        all_social.update(s for s in r.get("web_social", "").split("; ") if s)

        def _extend(target, key, dedup_field="name"):
            try:
                items = json.loads(r.get(key, "[]") or "[]")
                seen_keys = set()
                for x in target:
                    k = x.get(dedup_field) if isinstance(x, dict) and dedup_field else x
                    if k:
                        seen_keys.add(k)
                for item in items:
                    k = item.get(dedup_field) if isinstance(item, dict) and dedup_field else item
                    if k and k not in seen_keys:
                        target.append(item)
                        seen_keys.add(k)
            except Exception:
                pass

        _extend(all_amenities,    "web_amenities",        "name")
        _extend(all_services,     "web_special_services", "")
        _extend(all_faqs,         "web_faqs",             "question")
        _extend(all_team,         "web_team",             "name")
        _extend(all_testimonials, "web_testimonials",     "quote")

        try:
            for kw in json.loads(r.get("web_seo_keywords", "[]") or "[]"):
                if kw not in all_keywords:
                    all_keywords.append(kw)
        except Exception:
            pass

    merged["web_email"]            = "; ".join(sorted(all_emails))
    merged["web_extra_phones"]     = "; ".join(sorted(all_phones))
    merged["web_social"]           = "; ".join(sorted(all_social))
    merged["web_amenities"]        = json.dumps(all_amenities[:30],    ensure_ascii=False)
    merged["web_special_services"] = json.dumps(all_services[:50],     ensure_ascii=False)
    merged["web_faqs"]             = json.dumps(all_faqs[:20],         ensure_ascii=False)
    merged["web_team"]             = json.dumps(all_team[:20],         ensure_ascii=False)
    merged["web_testimonials"]     = json.dumps(all_testimonials[:20], ensure_ascii=False)
    merged["web_seo_keywords"]     = json.dumps(all_keywords[:80],     ensure_ascii=False)
    return merged


# ── Individual extraction helpers ──────────────────────────────────────────────

def _extract_facility_name(soup):
    og = soup.find("meta", property="og:title")
    if og and og.get("content"):
        return og["content"].strip()[:200]
    t = soup.find("title")
    if t:
        return t.get_text(" ", strip=True)[:200]
    h1 = soup.find("h1")
    return h1.get_text(" ", strip=True)[:200] if h1 else ""


def _extract_emails(soup, page_text):
    found = set()
    for a in soup.find_all("a", href=True):
        h = a["href"]
        if h.startswith("mailto:"):
            found.add(h[7:].split("?")[0].strip().lower())
    found.update(m.lower() for m in _EMAIL_RE.findall(page_text))
    return sorted(found - _SPAM_EMAILS)


def _extract_phones(page_text, existing_phone):
    norm = set()
    for r in _PHONE_RE.findall(page_text):
        n = normalize_phone(r)
        if n and n != existing_phone:
            norm.add(n)
    return sorted(norm)


def _extract_fax(soup, page_text):
    m = re.search(
        r"(?:fax|facsimile)[:\s#\-]*(\+?1?[\s.\-]?\(?\d{3}\)?[\s.\-]?\d{3}[\s.\-]?\d{4})",
        page_text, re.I
    )
    if m:
        return normalize_phone(m.group(1))
    for el in soup.find_all(string=re.compile(r"\bfax\b", re.I)):
        pm = _PHONE_RE.search(str(el.parent))
        if pm:
            return normalize_phone(pm.group())
    return ""


def _extract_social(soup):
    links = set()
    for a in soup.find_all("a", href=True):
        h = a["href"].lower().split("?")[0].rstrip("/")
        if any(d in h for d in _SOCIAL_DOMAINS):
            links.add(h)
    return sorted(links)


def _find_section(soup, keyword, min_len=40, limit=800):
    kw_re = re.compile(rf"\b{keyword}\b", re.I)

    def _valid(t):
        return len(t) > min_len and not _SECTION_NOISE.search(t)

    for tag in soup.find_all(["section", "div", "article"], id=kw_re):
        t = _clean(tag.get_text(" ", strip=True), limit)
        if _valid(t):
            return t
    for tag in soup.find_all(["section", "div", "article"], class_=kw_re):
        t = _clean(tag.get_text(" ", strip=True), limit)
        if _valid(t):
            return t
    for h in soup.find_all(["h1", "h2", "h3", "h4"]):
        if kw_re.search(h.get_text(" ", strip=True)):
            sib = h.find_next_sibling(["p", "div"])
            if sib:
                t = _clean(sib.get_text(" ", strip=True), limit)
                if _valid(t):
                    return t
    return ""


def _extract_about(soup, page_text=""):
    # 1. Named section containers
    for kw in ("about", "who-we-are", "our-story", "overview", "about-us"):
        r = _find_section(soup, kw, min_len=80)
        if r:
            return r

    # 2. First <article> with substantial text
    for article in soup.find_all("article"):
        t = _clean(article.get_text(" ", strip=True), 800)
        if len(t) > 120:
            return t

    # 3. Largest paragraph on the page (avoid nav/footer)
    for container in soup.find_all(["main", "section", "div"], limit=30):
        for p in container.find_all("p"):
            t = _clean(p.get_text(" ", strip=True), 800)
            if len(t) > 200:
                return t

    # 4. meta description as last resort
    for attr in ("og:description", "description"):
        el = (soup.find("meta", attrs={"name": attr})
              or soup.find("meta", property=attr))
        if el and el.get("content"):
            t = _clean(el["content"], 500)
            if len(t) > 60:
                return t

    return ""


def _extract_description(soup):
    for attr in ("og:description", "description"):
        el = (soup.find("meta", attrs={"name": attr})
              or soup.find("meta", property=attr))
        if el and el.get("content"):
            return _clean(el["content"], 300)
    return ""


def _extract_amenities(soup):
    items, seen = [], set()
    for kw in ("amenities", "amenity", "features", "services", "care"):
        containers = (soup.find_all(["section", "div", "ul"], id=re.compile(kw, re.I))
                      + soup.find_all(["section", "div", "ul"], class_=re.compile(kw, re.I)))
        for container in containers:
            for li in container.find_all("li"):
                name = _clean(li.get_text(" ", strip=True), 100)
                if not name or name in seen or len(name) < 3:
                    continue
                seen.add(name)
                desc_el = li.find_next_sibling(["p", "span"]) or li.find(["p", "span"])
                desc = _clean(desc_el.get_text(" ", strip=True), 200) if desc_el else ""
                if desc == name:
                    desc = ""
                items.append({"name": name, "description": desc})
            if items:
                break
        if items:
            break
    return items[:30]


def _extract_special_services(soup):
    services, seen = [], set()
    for kw in ("service", "care", "offering", "program"):
        for tag in (soup.find_all(["section", "div"], class_=re.compile(kw, re.I))
                    + soup.find_all(["section", "div"], id=re.compile(kw, re.I))):
            for p in tag.find_all(["p", "li"]):
                t = _clean(p.get_text(" ", strip=True), 300)
                if t and t not in seen and len(t) > 20:
                    seen.add(t)
                    services.append(t)
    return services[:50]


def _extract_faqs(soup):
    faqs, seen = [], set()

    def _add(q, a):
        # Strip UI noise like "Open Accordion Item" from questions
        q = _UI_NOISE.sub("", q).strip()
        q = _clean(q, 200)
        a = _clean(a, 500)
        if q and len(q) > 10 and q not in seen:
            seen.add(q)
            faqs.append({"question": q, "answer": a})

    # 1. JSON-LD FAQPage
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or "")
            items = data if isinstance(data, list) else [data]
            for item in items:
                if item.get("@type") == "FAQPage":
                    for entity in item.get("mainEntity", []):
                        q = entity.get("name", "")
                        a = entity.get("acceptedAnswer", {}).get("text", "")
                        _add(q, a)
        except Exception:
            pass

    # 2. HTML5 <details>/<summary>
    for details in soup.find_all("details"):
        summary = details.find("summary")
        if not summary:
            continue
        q = summary.get_text(" ", strip=True)
        a_parts = [c.get_text(" ", strip=True) for c in details.children
                   if c != summary and hasattr(c, "get_text")]
        _add(q, " ".join(a_parts))

    # 3. Container-based (dl/dt/dd, heading + sibling, aria-expanded)
    _FAQ_KW = re.compile(r"faq|question|accordion|toggl|collaps|answer", re.I)
    containers = []
    for tag in ["section", "div", "ul", "article"]:
        containers += soup.find_all(tag, id=_FAQ_KW)
        containers += soup.find_all(tag, class_=_FAQ_KW)
    for container in containers:
        for dt in container.find_all("dt"):
            dd = dt.find_next_sibling("dd")
            _add(dt.get_text(" ", strip=True), dd.get_text(" ", strip=True) if dd else "")
        for h in container.find_all(["h3", "h4", "h5", "button", "summary"]):
            q = h.get_text(" ", strip=True)
            sibling = h.find_next_sibling(["p", "div", "dd", "span"])
            _add(q, sibling.get_text(" ", strip=True) if sibling else "")

    if faqs:
        return faqs[:20]

    # 4. Global dl/dt/dd fallback
    for dt in soup.find_all("dt"):
        dd = dt.find_next_sibling("dd")
        _add(dt.get_text(" ", strip=True), dd.get_text(" ", strip=True) if dd else "")

    return faqs[:20]


def _extract_team(soup):
    team, seen = [], set()
    for kw in ("team", "staff", "leadership", "people", "meet"):
        containers = (soup.find_all(["section", "div"], id=re.compile(kw, re.I))
                      + soup.find_all(["section", "div"], class_=re.compile(kw, re.I)))
        for container in containers:
            for card in container.find_all(
                ["article", "div", "li"],
                class_=re.compile(r"(card|member|person|profile)", re.I)
            ):
                name_el = card.find(["h2", "h3", "h4", "strong"])
                name = _clean(name_el.get_text(" ", strip=True), 100) if name_el else ""
                if not name or name in seen:
                    continue
                seen.add(name)
                title_el = name_el.find_next_sibling(["p", "span"]) if name_el else None
                title = _clean(title_el.get_text(" ", strip=True), 100) if title_el else ""
                img = card.find("img")
                photo_url = ""
                if img:
                    photo_url = img.get("src") or img.get("data-src") or ""
                desc_el = card.find("p")
                desc = _clean(desc_el.get_text(" ", strip=True), 300) if desc_el else ""
                team.append({"name": name, "title": title,
                             "photo_url": photo_url, "description": desc})
    return team[:20]


def _is_real_testimonial(quote: str, author: str, rating: str) -> bool:
    """Filter out page headers and marketing copy masquerading as testimonials."""
    if len(quote) < 40:
        return False
    # Must have first-person language OR author attribution OR a star rating
    has_first_person = bool(_FIRST_PERSON.search(quote))
    has_author       = bool(author and len(author.strip()) > 2)
    has_rating       = bool(rating and rating.strip())
    if not (has_first_person or has_author or has_rating):
        return False
    # Reject if quote looks like a heading (all words capitalized, no punctuation)
    words = quote.split()
    if len(words) <= 8:
        title_case_ratio = sum(1 for w in words if w and w[0].isupper()) / len(words)
        if title_case_ratio > 0.7 and not re.search(r"[.!?,;]", quote):
            return False
    return True


def _extract_testimonials(soup):
    testimonials, seen = [], set()

    def _add(quote, author="", role="", rating="", source="html"):
        quote = _clean(quote, 500)
        if not quote or quote in seen:
            return
        if not _is_real_testimonial(quote, author, rating):
            return
        seen.add(quote)
        testimonials.append({
            "quote":  quote,
            "author": _clean(author, 100),
            "role":   _clean(role, 100),
            "rating": str(rating),
            "source": source,
        })

    # 1. JSON-LD Review structured data
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or "")
            items = data if isinstance(data, list) else [data]
            for item in items:
                reviews = item.get("review", [])
                if isinstance(reviews, dict):
                    reviews = [reviews]
                for r in reviews:
                    quote  = r.get("reviewBody", "")
                    author = r.get("author", {})
                    author_name = author.get("name", "") if isinstance(author, dict) else str(author)
                    rating_obj  = r.get("reviewRating", {})
                    rating = rating_obj.get("ratingValue", "") if isinstance(rating_obj, dict) else ""
                    _add(quote, author_name, "", rating, "json-ld")
        except Exception:
            pass

    # 2. Container-based
    _T_KW   = re.compile(
        r"testimonial|review|quote|slider|carousel|what.people|say|feedback|stories|resident",
        re.I
    )
    _CARD_KW = re.compile(r"quote|card|review|testimonial|item|slide|entry", re.I)
    containers = []
    for tag in ["section", "div", "article", "ul"]:
        containers += soup.find_all(tag, id=_T_KW)
        containers += soup.find_all(tag, class_=_T_KW)

    for container in containers:
        cards = (container.find_all(["blockquote", "div", "article", "li"], class_=_CARD_KW)
                 or container.find_all("blockquote")
                 or [container])
        for card in cards:
            quote_el = (card.find(["blockquote", "q"])
                        or card.find(class_=re.compile(r"quote|body|text|content", re.I))
                        or card.find("p"))
            quote = quote_el.get_text(" ", strip=True) if quote_el else ""
            # Handle malformed HTML: empty <p class="..."><p>actual text</p>
            if not quote and quote_el is not None:
                sib = quote_el.find_next_sibling("p")
                if sib:
                    quote = sib.get_text(" ", strip=True)
            if not quote:
                quote = card.get_text(" ", strip=True)

            author_el = (card.find(["cite"])
                         or card.find(class_=re.compile(r"author|name|person|resident|client", re.I))
                         or card.find(["strong", "b"]))
            author = author_el.get_text(" ", strip=True) if author_el else ""

            role_el = card.find(class_=re.compile(r"role|title|position|relation", re.I))
            role    = role_el.get_text(" ", strip=True) if role_el else ""

            rating = ""
            stars_el = card.find(attrs={"aria-label": re.compile(r"\d.*star", re.I)})
            if stars_el:
                m = re.search(r"(\d[\d.]*)", stars_el.get("aria-label", ""))
                if m:
                    rating = m.group(1)

            _add(quote, author, role, rating, "card")

    if testimonials:
        return testimonials[:20]

    # 3. Global <blockquote> fallback
    for bq in soup.find_all("blockquote"):
        cite   = bq.find("cite")
        author = cite.get_text(" ", strip=True) if cite else ""
        _add(bq.get_text(" ", strip=True), author, source="blockquote")

    return testimonials[:20]


def _extract_seo_keywords(soup, page_text):
    keywords = []
    meta_kw = soup.find("meta", attrs={"name": "keywords"})
    if meta_kw and meta_kw.get("content"):
        keywords.extend(k.strip() for k in meta_kw["content"].split(",") if k.strip())
    for h in soup.find_all(["h1", "h2", "h3"]):
        t = _clean(h.get_text(" ", strip=True), 120)
        if t and t not in keywords:
            keywords.append(t)
    stopwords = {
        "that", "this", "with", "from", "have", "will", "your", "more", "also",
        "their", "they", "what", "when", "where", "which", "about", "been",
        "home", "each", "into", "after", "before", "some", "them", "then", "than",
    }
    freq: dict = {}
    for w in re.findall(r"\b[a-zA-Z]{4,}\b", page_text):
        wl = w.lower()
        if wl not in stopwords:
            freq[wl] = freq.get(wl, 0) + 1
    top_words = sorted(freq, key=lambda x: -freq[x])[:30]
    keywords.extend(w for w in top_words if w not in keywords)
    return keywords[:80]


# ══════════════════════════════════════════════════════════════════════════════
# WEBSITE SCRAPING — requests path (primary)
# ══════════════════════════════════════════════════════════════════════════════

def _fetch_html_requests(url: str) -> str:
    try:
        r = requests.get(url, headers=WEB_HEADERS, timeout=WEB_TIMEOUT, allow_redirects=True)
        r.raise_for_status()
        return r.text
    except Exception:
        return ""


def _has_meaningful_content(soup) -> bool:
    """Check that the page has real content, not just nav/footer/scripts."""
    # Strip nav, footer, header, scripts, styles
    for tag in soup(["nav", "footer", "header", "script", "style", "noscript"]):
        tag.decompose()
    text = soup.get_text(" ", strip=True)
    return len(text) >= MIN_CONTENT_LEN


def scrape_website_requests(url: str, existing_phone: str = "") -> tuple:
    """
    Returns (merged_result_dict, pages_scraped, is_content_sufficient).
    """
    html = _fetch_html_requests(url)
    if not html:
        return {}, 0, False

    soup = BeautifulSoup(html, "html.parser")
    sufficient = _has_meaningful_content(BeautifulSoup(html, "html.parser"))

    page_results = [_parse_soup(soup, existing_phone)]
    pages_scraped = 1

    sub_urls = _get_sub_page_urls(soup, url)
    for sub_url in sub_urls:
        sub_html = _fetch_html_requests(sub_url)
        if sub_html:
            sub_soup = BeautifulSoup(sub_html, "html.parser")
            page_results.append(_parse_soup(sub_soup, existing_phone))
            pages_scraped += 1

    return _merge_results(page_results), pages_scraped, sufficient


# ══════════════════════════════════════════════════════════════════════════════
# WEBSITE SCRAPING — Selenium path (JS-heavy sites fallback)
# ══════════════════════════════════════════════════════════════════════════════

def _new_selenium_driver(headless: bool = True) -> webdriver.Chrome:
    opts = ChromeOptions()
    if headless:
        opts.add_argument("--headless=new")
    opts.add_argument("--incognito")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--disable-blink-features=AutomationControlled")
    opts.add_argument("--window-size=1920,1080")
    opts.add_argument(f"--user-agent={UA}")
    opts.add_experimental_option("excludeSwitches", ["enable-automation"])
    driver = webdriver.Chrome(options=opts)
    driver.set_page_load_timeout(30)
    return driver


def scrape_website_selenium(url: str, existing_phone: str = "",
                            headless: bool = True) -> tuple:
    """Fresh Chrome per site. Returns (merged_result_dict, pages_scraped)."""
    driver = None
    try:
        driver = _new_selenium_driver(headless)
    except Exception as e:
        log.debug("Selenium driver launch failed: %s", e)
        return {}, 0

    page_results = []
    pages_scraped = 0

    def _fetch(u: str) -> str:
        try:
            driver.get(u)
            _time.sleep(3)
            return driver.page_source
        except Exception as ex:
            log.debug("Selenium fetch failed — %s: %s", u, ex)
            return ""

    try:
        html = _fetch(url)
        if not html:
            return {}, 0

        soup = BeautifulSoup(html, "html.parser")
        page_results.append(_parse_soup(soup, existing_phone))
        pages_scraped = 1

        sub_urls = _get_sub_page_urls(soup, url)
        for sub_url in sub_urls:
            sub_html = _fetch(sub_url)
            if sub_html:
                sub_soup = BeautifulSoup(sub_html, "html.parser")
                page_results.append(_parse_soup(sub_soup, existing_phone))
                pages_scraped += 1

    finally:
        try:
            driver.quit()
        except Exception:
            pass

    return _merge_results(page_results), pages_scraped


# ══════════════════════════════════════════════════════════════════════════════
# WEBSITE SCRAPING — unified entry point
# ══════════════════════════════════════════════════════════════════════════════

def scrape_website_sync(url: str, existing_phone: str = "",
                        headless: bool = True) -> dict:
    """
    1. Try requests (fast)
    2. If content insufficient → fallback to Selenium
    Returns flat dict with all web_* fields.
    """
    empty = {
        "web_facility_name": "", "web_email": "", "web_fax": "",
        "web_extra_phones": "", "web_social": "",
        "web_about": "", "web_mission": "", "web_vision": "", "web_description": "",
        "web_amenities": "[]", "web_special_services": "[]",
        "web_faqs": "[]", "web_team": "[]",
        "web_testimonials": "[]", "web_seo_keywords": "[]",
        "web_pages_scraped": "0", "web_extraction_status": "failed",
    }
    if not url:
        return empty

    result, pages, sufficient = scrape_website_requests(url, existing_phone)

    if not sufficient:
        log.debug("Requests insufficient for %s — using Selenium", url)
        result, pages = scrape_website_selenium(url, existing_phone, headless)

    if not result:
        return empty

    has_content = any([
        result.get("web_about"), result.get("web_mission"),
        result.get("web_vision"), result.get("web_description"),
        result.get("web_amenities", "[]") not in ("[]", ""),
        result.get("web_faqs", "[]") not in ("[]", ""),
        result.get("web_testimonials", "[]") not in ("[]", ""),
    ])
    result["web_pages_scraped"]     = str(pages)
    result["web_extraction_status"] = "success" if has_content else "partial"
    result.pop("_page_text_len", None)
    return result


async def scrape_website(url: str, existing_phone: str = "",
                         headless: bool = True) -> dict:
    async with _WEB_SEM:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            None, scrape_website_sync, url, existing_phone, headless
        )


# ══════════════════════════════════════════════════════════════════════════════
# PLAYWRIGHT BROWSER HELPER
# ══════════════════════════════════════════════════════════════════════════════

async def new_browser(p, headless: bool):
    browser = await p.chromium.launch(headless=headless, slow_mo=40, args=BROWSER_ARGS)
    ctx  = await browser.new_context(viewport={"width": 1920, "height": 1080}, user_agent=UA)
    page = await ctx.new_page()
    await page.add_init_script(
        "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
    )
    return browser, page


# ══════════════════════════════════════════════════════════════════════════════
# PHASE 2 WORKER — Maps detail only
# ══════════════════════════════════════════════════════════════════════════════

async def process_city_maps(city_slug: str, facilities: list,
                            headless: bool, query_slug: str = "assisted_living"):
    """Visits Maps URLs and extracts detail. Writes to outputs/phase2/."""
    if not facilities:
        return

    PHASE2_DIR.mkdir(exist_ok=True)
    csv_path = PHASE2_DIR / f"{city_slug}_phase2_{query_slug}.csv"
    csv_file = open(csv_path, "w", newline="", encoding="utf-8")
    writer   = csv.DictWriter(csv_file, fieldnames=PHASE2_FIELDS)
    writer.writeheader()
    csv_file.flush()

    browser = page = None
    count = 0

    try:
        async with async_playwright() as p:
            for idx, fac in enumerate(facilities, start=1):
                maps_url = fac.get("maps_url", "")
                name     = fac.get("name", f"Facility {idx}")

                if browser is None or (idx - 1) % RESTART_EVERY == 0:
                    if browser:
                        try:
                            await browser.close()
                        except Exception:
                            pass
                        log.info("  [%s|maps] Browser restarted at #%d", city_slug, idx)
                    try:
                        browser, page = await new_browser(p, headless)
                    except Exception as e:
                        log.error("  [%s|maps] Browser launch failed: %s", city_slug, e)
                        break

                log.info("[%s|maps] %d/%d — %s", city_slug, idx, len(facilities), name)
                detail = await scrape_maps_detail(page, maps_url, name)

                for dst, src in (
                    ("name", "name"), ("rating", "rating"), ("reviews", "reviews"),
                    ("category", "category"), ("address_lane1", "address_lane1"),
                    ("phone", "phone"), ("maps_status", "status"),
                    ("website", "website"), ("maps_url", "maps_url"),
                ):
                    if not detail.get(dst) and fac.get(src):
                        detail[dst] = fac[src]

                writer.writerow({f: detail.get(f, "") for f in PHASE2_FIELDS})
                csv_file.flush()
                count += 1
                log.info("  ✓ [%s|maps] %d/%d saved", city_slug, idx, len(facilities))
                await page.wait_for_timeout(DELAY_MS)

    finally:
        if browser:
            try:
                await browser.close()
            except Exception:
                pass
        csv_file.close()
        log.info("Maps done: %s → %d records → %s", city_slug, count, csv_path.name)


# ══════════════════════════════════════════════════════════════════════════════
# EXTRACTION WORKER — Website scraping only
# ══════════════════════════════════════════════════════════════════════════════

async def process_city_website(city_slug: str, query_slug: str = "assisted_living",
                               web_headless: bool = True):
    """Reads Phase 2 CSV (falls back to Phase 1), scrapes websites, writes to outputs/extraction/."""
    phase2_csv = PHASE2_DIR / f"{city_slug}_phase2_{query_slug}.csv"
    phase1_csv = PHASE1_DIR / f"{city_slug}_phase1_{query_slug}.csv"

    if phase2_csv.exists():
        src_csv = phase2_csv
    elif phase1_csv.exists():
        log.info("[%s|web] Phase2 not ready — reading from phase1: %s", city_slug, phase1_csv.name)
        src_csv = phase1_csv
    else:
        log.error("[%s|web] No phase1 or phase2 CSV found for %s", city_slug, query_slug)
        return

    with open(src_csv, newline="", encoding="utf-8") as f:
        facilities = list(csv.DictReader(f))
    if not facilities:
        return

    EXTRACTION_DIR.mkdir(exist_ok=True)
    csv_path = EXTRACTION_DIR / f"{city_slug}_extraction_{query_slug}.csv"
    csv_file = open(csv_path, "w", newline="", encoding="utf-8")
    writer   = csv.DictWriter(csv_file, fieldnames=EXTRACTION_FIELDS)
    writer.writeheader()
    csv_file.flush()

    count = 0
    try:
        for idx, fac in enumerate(facilities, start=1):
            name        = fac.get("name", f"Facility {idx}")
            website_url = fac.get("website", "")
            log.info("[%s|web] %d/%d — %s", city_slug, idx, len(facilities), name)

            row = dict(fac)
            if website_url:
                web_data = await scrape_website(website_url, fac.get("phone", ""), web_headless)
            else:
                web_data = scrape_website_sync("")
            row.update(web_data)

            writer.writerow({f: row.get(f, "") for f in EXTRACTION_FIELDS})
            csv_file.flush()
            count += 1
            log.info("  ✓ [%s|web] %d/%d saved", city_slug, idx, len(facilities))

    finally:
        csv_file.close()
        log.info("Web done: %s → %d records → %s", city_slug, count, csv_path.name)


# ══════════════════════════════════════════════════════════════════════════════
# ORCHESTRATORS
# ══════════════════════════════════════════════════════════════════════════════

def _slug(s: str) -> str:
    return s.lower().replace(" ", "_").replace(".", "")


def _phase1_work(city_filter: str, limit: int) -> list:
    """Return [(city_slug, query_slug, facilities), ...] from phase1 CSVs."""
    city_slug_filter = _slug(city_filter) if city_filter else ""
    work = []
    for query_slug in QUERY_SLUGS:
        files = sorted(PHASE1_DIR.glob(f"*_phase1_{query_slug}.csv"))
        if city_slug_filter:
            files = [f for f in files if f.stem.startswith(f"{city_slug_filter}_")]
        for p1 in files:
            cs = p1.stem.replace(f"_phase1_{query_slug}", "")
            with open(p1, newline="", encoding="utf-8") as f:
                rows = list(csv.DictReader(f))
            seen, unique = set(), []
            for fac in rows:
                u = fac.get("maps_url", "")
                if u and u not in seen:
                    seen.add(u)
                    unique.append(fac)
            if limit:
                unique = unique[:limit]
            if unique:
                work.append((cs, query_slug, unique))
    return work


async def run_maps(headless: bool = True, workers: int = MAPS_WORKERS,
                   city_filter: str = "", limit: int = 0):
    """Phase 2 — Maps detail. Reads phase1 CSVs → writes phase2 CSVs."""
    OUTPUT_DIR.mkdir(exist_ok=True)
    PHASE2_DIR.mkdir(exist_ok=True)

    work = []
    for cs, qs, unique in _phase1_work(city_filter, limit):
        p2 = PHASE2_DIR / f"{cs}_phase2_{qs}.csv"
        p1_count = len(unique)
        if p2.exists():
            p2_rows = sum(1 for _ in p2.open(encoding="utf-8")) - 1
            if p2_rows >= p1_count > 0:
                log.info("Skip maps (done): %s | %s (%d rows)", cs, qs, p2_rows)
                continue
            log.info("Incomplete maps — redo: %s | %s", cs, qs)
            p2.unlink()
        work.append((cs, qs, unique))

    if not work:
        log.info("All phase2 maps already done.")
        return

    log.info("%d jobs — %d Playwright workers", len(work), workers)
    sem = asyncio.Semaphore(workers)

    async def run(cs, qs, facs):
        async with sem:
            await process_city_maps(cs, facs, headless, query_slug=qs)

    await asyncio.gather(*[run(cs, qs, facs) for cs, qs, facs in work])
    log.info("Phase 2 maps complete.")


async def run_website(web_headless: bool = True, workers: int = MAPS_WORKERS,
                      city_filter: str = "", limit: int = 0):
    """Extraction — website only. Reads phase2 CSVs → writes extraction CSVs."""
    global _WEB_SEM
    _WEB_SEM = asyncio.Semaphore(workers)
    OUTPUT_DIR.mkdir(exist_ok=True)
    EXTRACTION_DIR.mkdir(exist_ok=True)

    city_slug_filter = _slug(city_filter) if city_filter else ""
    work = []
    seen_jobs = set()
    for query_slug in QUERY_SLUGS:
        # Collect city slugs from phase2 OR phase1
        p2_files = sorted(PHASE2_DIR.glob(f"*_phase2_{query_slug}.csv"))
        p1_files = sorted(PHASE1_DIR.glob(f"*_phase1_{query_slug}.csv"))
        all_slugs = set()
        for f in p2_files:
            all_slugs.add(f.stem.replace(f"_phase2_{query_slug}", ""))
        for f in p1_files:
            all_slugs.add(f.stem.replace(f"_phase1_{query_slug}", ""))
        if city_slug_filter:
            all_slugs = {s for s in all_slugs if s == city_slug_filter}
        for cs in sorted(all_slugs):
            if (cs, query_slug) in seen_jobs:
                continue
            seen_jobs.add((cs, query_slug))
            src = (PHASE2_DIR / f"{cs}_phase2_{query_slug}.csv"
                   if (PHASE2_DIR / f"{cs}_phase2_{query_slug}.csv").exists()
                   else PHASE1_DIR / f"{cs}_phase1_{query_slug}.csv")
            src_rows = sum(1 for _ in src.open(encoding="utf-8")) - 1
            ext = EXTRACTION_DIR / f"{cs}_extraction_{query_slug}.csv"
            if ext.exists():
                ext_rows = sum(1 for _ in ext.open(encoding="utf-8")) - 1
                if ext_rows >= src_rows > 0:
                    log.info("Skip extraction (done): %s | %s (%d rows)", cs, query_slug, ext_rows)
                    continue
                log.info("Incomplete extraction — redo: %s | %s", cs, query_slug)
                ext.unlink()
            work.append((cs, query_slug))

    if not work:
        log.info("All extractions already done.")
        return

    log.info("%d jobs — %d worker slots", len(work), workers)
    sem = asyncio.Semaphore(workers)

    async def run(cs, qs):
        async with sem:
            await process_city_website(cs, qs, web_headless)

    await asyncio.gather(*[run(cs, qs) for cs, qs in work])
    log.info("Website extraction complete.")


async def run_all(headless: bool = True, web_headless: bool = True,
                  workers: int = MAPS_WORKERS, city_filter: str = "", limit: int = 0):
    """Run Maps phase then Website extraction in sequence."""
    await run_maps(headless=headless, workers=workers,
                   city_filter=city_filter, limit=limit)
    await run_website(web_headless=web_headless, workers=workers,
                      city_filter=city_filter, limit=limit)


# ══════════════════════════════════════════════════════════════════════════════
# ENTRY POINT
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    headless     = "--visible"     not in sys.argv
    web_headless = "--web-visible" not in sys.argv
    maps_only    = "--maps"    in sys.argv
    website_only = "--website" in sys.argv

    workers_arg = next(
        (int(a.split("=")[1]) for a in sys.argv[1:] if a.startswith("--workers=")),
        MAPS_WORKERS,
    )
    limit_arg = next((int(a) for a in sys.argv[1:] if a.isdigit()), 0)
    city_arg  = next(
        (a for a in sys.argv[1:] if not a.startswith("--") and not a.isdigit()), ""
    )

    if maps_only:
        asyncio.run(run_maps(headless=headless, workers=workers_arg,
                             city_filter=city_arg, limit=limit_arg))
    elif website_only:
        asyncio.run(run_website(web_headless=web_headless, workers=workers_arg,
                                city_filter=city_arg, limit=limit_arg))
    else:
        asyncio.run(run_all(headless=headless, web_headless=web_headless,
                            workers=workers_arg, city_filter=city_arg, limit=limit_arg))
