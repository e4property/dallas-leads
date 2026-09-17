"""
Dallas County Foreclosure Notice Scraper v1

Data source: dallas.tx.publicsearch.us, department=FC (Foreclosures) -- the
same Tyler PublicSearch platform Bexar uses, and (confirmed live 2026-09-17)
the exact same table column layout: col-3=doc type, col-4=recorded date,
col-5=sale date, col-6=doc number, col-8=property address. Confirmed via the
county's own site (dallascounty.org/.../foreclosures.php): notices filed on
or after 2/24/2026 live here; older ones are individual PDFs on that page,
not handled by this scraper.

Key difference from Bexar: this department does NOT expose owner name or a
real street address as structured fields -- "Property Address" here is just
the city (e.g. "DALLAS"). The real address and grantor/borrower name only
exist inside the scanned document image itself, so this scraper downloads
page 1 of each new qualifying notice and OCRs it (pytesseract) to pull
"commonly known as <address>" and the Grantor(s) name out of the body text.
No Dallas CAD/owner-lookup integration yet (Bexar has one via Harris Govern;
Dallas would need its own, separate project -- not built here).

Per explicit instruction: ONLY notices with a sale date still in the future
are kept. Past-auction rows are discarded at scrape time (not just relying
on the purge script) -- verified live 2026-09-17 that the current dataset's
~2,501 notices (bulk-migrated 2/24/2026) mostly carry sale dates already in
the past (Apr-Jun 2026); very few or none may qualify as "future" until
Dallas posts its next batch (Texas law only requires a foreclosure notice be
posted ~21 days before the sale, so notices for e.g. an October auction
would only appear a few weeks out -- an empty/near-empty first run is
expected, not a bug).
"""

import json
import logging
import os
import re
import time
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s.%(msecs)03d [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

PUBLICSEARCH_BASE = "https://dallas.tx.publicsearch.us"
RECORDS_PATH = Path("dashboard/records.json")

TODAY_CT = datetime.now(ZoneInfo("America/Chicago")).replace(tzinfo=None)
RUN_TIMESTAMP = datetime.now(ZoneInfo("UTC")).strftime("%Y-%m-%dT%H:%M:%SZ")

# 2/24/2026 is the county's own stated migration date onto this platform
# (dallascounty.org/.../foreclosures.php) -- nothing real exists before it
# here. End of window is padded 220 days out so a run never has to guess
# how far in advance notices might eventually get posted.
SEARCH_START = "20260201"
SEARCH_END   = (TODAY_CT + timedelta(days=220)).strftime("%Y%m%d")

PAGE_TIMEOUT = 120
MAX_PAGES    = 60          # 60*50 = 3000 rows -- comfortably above the current ~2,501 total
OCR_LIMIT    = 60          # per-run cap on new docs OCR'd -- backlog carries over via known_docs


def get_driver():
    from selenium import webdriver
    from selenium.webdriver.chrome.options import Options

    opts = Options()
    opts.add_argument("--headless=new")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--disable-gpu")
    opts.add_argument("--window-size=1920,1080")
    opts.add_argument("--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36")
    opts.add_argument("--disable-blink-features=AutomationControlled")
    opts.add_experimental_option("excludeSwitches", ["enable-automation"])
    opts.add_experimental_option("useAutomationExtension", False)

    try:
        from selenium.webdriver.chrome.service import Service as ChromeService
        from webdriver_manager.chrome import ChromeDriverManager
        service = ChromeService(ChromeDriverManager().install())
        driver = webdriver.Chrome(service=service, options=opts)
    except Exception:
        driver = webdriver.Chrome(options=opts)

    driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {
        "source": """
            Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
            Object.defineProperty(navigator, 'plugins', {get: () => [1,2,3]});
            Object.defineProperty(navigator, 'languages', {get: () => ['en-US','en']});
            window.chrome = {runtime: {}};
        """
    })
    return driver


def parse_mdy(s):
    if not s:
        return None
    try:
        return datetime.strptime(s.strip(), "%m/%d/%Y")
    except Exception:
        return None


def load_known_docs():
    if RECORDS_PATH.exists():
        try:
            prev = json.loads(RECORDS_PATH.read_text(encoding="utf-8"))
            return {r["doc_number"] for r in prev if r.get("doc_number")}, prev
        except Exception as e:
            log.warning(f"Could not load existing records.json: {e}")
    return set(), []


def scrape_search_results(driver):
    """
    Paginate department=FC for the full search window, returning every
    NOTICE OF FORECLOSURE row with a still-future sale date. Past-auction
    rows are discarded right here, not just left for the purge script --
    per explicit instruction, this scraper should never even keep them
    long enough for the dashboard to show them for a moment.
    """
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support.ui import WebDriverWait

    search_url = (
        f"{PUBLICSEARCH_BASE}/results"
        f"?department=FC"
        f"&instrumentDateRange={SEARCH_START}%2C{SEARCH_END}"
        f"&keywordSearch=false"
        f"&limit=50"
        f"&sort=desc"
        f"&sortBy=recordedDate"
        f"&sortDir=desc"
        f"&searchType=advancedSearch"
    )

    future_rows = []
    seen_docs = set()
    offset = 0
    page = 0
    prev_page_was_full = False

    while True:
        if page + 1 > MAX_PAGES:
            log.warning(f"Hit MAX_PAGES={MAX_PAGES} -- stopping, rest deferred to next run")
            break
        url = f"{search_url}&offset={offset}"
        log.info(f"Page {page + 1} (offset={offset})")

        loaded = False
        for attempt in range(2):
            try:
                driver.set_page_load_timeout(PAGE_TIMEOUT)
                driver.get(url)
                WebDriverWait(driver, PAGE_TIMEOUT).until(
                    lambda d: (
                        d.find_elements(By.CSS_SELECTOR, "table tbody tr")
                        or d.find_elements(By.XPATH, "//h1[contains(text(),'No Results')]")
                    )
                )
                time.sleep(1.5)
                loaded = True
                break
            except Exception:
                log.info(f"  Timeout attempt {attempt + 1} -- {'retrying' if attempt == 0 else 'stopping'}")
                if attempt == 0:
                    time.sleep(5)
        if not loaded:
            log.info("  Timeout -- stopping")
            break

        rows = driver.find_elements(By.CSS_SELECTOR, "table tbody tr")
        if not rows:
            if driver.find_elements(By.XPATH, "//h1[contains(text(),'No Results')]"):
                log.info("  No results")
            else:
                time.sleep(3)
                rows = driver.find_elements(By.CSS_SELECTOR, "table tbody tr")
        if not rows:
            log.info("  No rows -- stopping")
            break
        prev_page_was_full = len(rows) >= 48

        page_new = 0
        for row in rows:
            try:
                data = driver.execute_script(
                    """
                    const row = arguments[0];
                    const out = {};
                    for (const c of ['col-3','col-4','col-5','col-6','col-8']) {
                        const el = row.querySelector('td.' + c);
                        out[c] = el ? el.innerText : '';
                    }
                    const a = row.querySelector('a');
                    out['href'] = a ? a.getAttribute('href') : '';
                    return out;
                    """,
                    row,
                ) or {}
                doc_type = (data.get("col-3") or "").strip().upper()
                recorded_date = (data.get("col-4") or "").strip()
                sale_date = (data.get("col-5") or "").strip()
                doc_number = (data.get("col-6") or "").strip()
                city = (data.get("col-8") or "").strip()
                href = (data.get("href") or "").strip()

                if not doc_number or doc_number in seen_docs:
                    continue
                if "FORECLOSURE" not in doc_type:
                    continue  # skip VOID FC etc.
                sale_dt = parse_mdy(sale_date)
                if not sale_dt or sale_dt.date() < TODAY_CT.date():
                    continue  # past auction -- discard, per explicit instruction

                seen_docs.add(doc_number)
                future_rows.append({
                    "doc_number": doc_number,
                    "recorded_date": recorded_date,
                    "sale_date": sale_date,
                    "city": city,
                    "href": href,
                })
                page_new += 1
            except Exception as e:
                log.debug(f"  Row parse error: {e}")

        log.info(f"  Page {page + 1}: {page_new} future-dated new rows (running total {len(future_rows)})")
        if len(rows) < 48:
            break
        offset += 50
        page += 1
        time.sleep(1)

    return future_rows


ADDRESS_RE = re.compile(
    r"commonly known as[:\s]+([0-9][^\n,]*(?:,[^\n]*)?(?:TX|Texas)[^\n]*\d{5})",
    re.IGNORECASE,
)
ADDRESS_FALLBACK_RE = re.compile(
    r"Property Address:?\s*([0-9][^\n]{5,80})",
    re.IGNORECASE,
)
GRANTOR_RE = re.compile(
    r"Grantor\(?s?\)?:?\s*([A-Z][A-Za-z .,&'\-]{3,80})",
)


def ocr_doc(driver, href):
    """
    Navigate to the doc detail page, grab page 1's signed image URL, OCR it.
    Returns (address, owner) -- either may be "" if the template didn't
    match or OCR came back too noisy to parse.
    """
    from selenium.webdriver.common.by import By

    try:
        driver.set_page_load_timeout(PAGE_TIMEOUT)
        driver.get(f"{PUBLICSEARCH_BASE}{href}")
        time.sleep(3)
        img = None
        for _ in range(6):
            imgs = driver.find_elements(By.CSS_SELECTOR, "img[src*='/files/documents/']")
            if imgs:
                img = imgs[0]
                break
            time.sleep(2)
        if not img:
            return "", ""
        img_url = img.get_attribute("src")
        if not img_url:
            return "", ""

        import tempfile
        req = urllib.request.Request(img_url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=30) as r:
            image_bytes = r.read()
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
            tmp.write(image_bytes)
            tmp_path = tmp.name

        import pytesseract
        from PIL import Image
        text = pytesseract.image_to_string(Image.open(tmp_path))
        try:
            os.unlink(tmp_path)
        except Exception:
            pass

        addr_match = ADDRESS_RE.search(text) or ADDRESS_FALLBACK_RE.search(text)
        address = addr_match.group(1).strip().rstrip(".") if addr_match else ""
        grantor_match = GRANTOR_RE.search(text)
        owner = grantor_match.group(1).strip().rstrip(".") if grantor_match else ""
        return address, owner
    except Exception as e:
        log.debug(f"  OCR error for {href}: {e}")
        return "", ""


def parse_city_zip(address_or_city):
    m = re.search(r"([A-Za-z .]+),?\s*TX\.?\s*(\d{5})", address_or_city, re.IGNORECASE)
    if m:
        return m.group(1).strip().upper(), m.group(2)
    return "", ""


def score_record(rec):
    score = 5
    if rec.get("sale_date"):
        score += 3
    if rec.get("owner"):
        score += 1
    if rec.get("address") and rec["address"] != rec.get("city"):
        score += 1
    return score


def build_record(row, address, owner):
    sale_dt = parse_mdy(row["sale_date"])
    days_until = (sale_dt.date() - TODAY_CT.date()).days if sale_dt else None
    city, zip_code = parse_city_zip(address) if address else (row.get("city", ""), "")

    rec = {
        "type": "NOF",
        "source": "dallas_publicsearch",
        "county": "dallas",
        "owner": owner.title() if owner else "",
        "address": address if address else row.get("city", ""),
        "mail_addr": "",
        "city": city or row.get("city", ""),
        "zip": zip_code,
        "absentee": False,
        "duplicate": False,
        "is_new": True,
        "doc_number": row["doc_number"],
        "date_filed": row["recorded_date"],
        "date_recorded": row["recorded_date"],
        "sale_date": row["sale_date"],
        "days_until_sale": days_until,
        "run_ts": RUN_TIMESTAMP,
        "flags": ["NEW", "HAS SALE DATE"] + (["NO ADDRESS - OCR MISS"] if not address else []) + (["NO OWNER - OCR MISS"] if not owner else []),
        "lender": "",
        "loan_amount": "",
        "loan_date": "",
        "trustee": "",
        "tenure_years": None,
        "prop_id": "",
        "deed_date": "",
        "appraised_value": "",
        "annual_taxes": "",
        "land_value": "",
        "stacked": False,
        "ghl_pushed": False,
        "ghl_pushed_at": "",
        "ghl_id": "",
        "dash_phone": "",
        "dash_dispo": "new",
        "dash_notes": "",
    }
    rec["score"] = score_record(rec)
    return rec


def main():
    known_docs, prev_records = load_known_docs()
    log.info(f"Loaded {len(prev_records)} existing records ({len(known_docs)} known doc numbers)")

    driver = get_driver()
    try:
        future_rows = scrape_search_results(driver)
        log.info(f"Found {len(future_rows)} total future-dated FORECLOSURE notices in window")

        new_rows = [r for r in future_rows if r["doc_number"] not in known_docs]
        log.info(f"{len(new_rows)} are new (not already in records.json)")

        new_records = []
        for i, row in enumerate(new_rows[:OCR_LIMIT]):
            log.info(f"[{i + 1}/{min(len(new_rows), OCR_LIMIT)}] OCR doc {row['doc_number']} (sale {row['sale_date']})")
            address, owner = ocr_doc(driver, row["href"]) if row["href"] else ("", "")
            rec = build_record(row, address, owner)
            new_records.append(rec)
            time.sleep(1)
    finally:
        driver.quit()

    # Drop any existing record whose sale date has since passed -- belt and
    # suspenders alongside purge_past_auctions.py, since this scraper's own
    # job is specifically "future auctions only."
    kept_prev = []
    for r in prev_records:
        sd = parse_mdy(r.get("sale_date", ""))
        if sd and sd.date() < TODAY_CT.date():
            continue
        kept_prev.append(r)
    dropped = len(prev_records) - len(kept_prev)
    if dropped:
        log.info(f"Dropped {dropped} existing record(s) whose sale date has passed")

    all_records = kept_prev + new_records
    RECORDS_PATH.parent.mkdir(parents=True, exist_ok=True)
    RECORDS_PATH.write_text(json.dumps(all_records, indent=2, ensure_ascii=False), encoding="utf-8")
    log.info(f"Saved {len(all_records)} records ({len(new_records)} new)")
    print(f"new={len(new_records)}")


if __name__ == "__main__":
    main()
