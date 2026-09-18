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
OCR_LIMIT    = 5           # TEMP diagnostic run 2026-09-18, restore to 30 after root cause confirmed
                            # Each OCR fetch reloads a full results page to reach a clickable row
                            # (see ocr_doc below), so this is deliberately conservative -- 2026-09-17
                            # test run's rapid page loads got a real "request timed out" from the
                            # county's own site under load; don't hammer it harder than this.

SEARCH_URL = (
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
    """Returns (known_good_docs, prev_records). known_good_docs is doc
    numbers that already have a real address or owner -- genuinely done,
    skip forever. Records with neither (every doc from this scraper's whole
    two-prior-attempt 0%-OCR history) are deliberately left OUT of that
    set: confirmed live 2026-09-18 that treating "already has a row in
    records.json" as "done" meant a fresh run found 0 new docs and never
    retried a single one of the 79 already sitting there with blank OCR
    data. Those get re-fetched (using the current run's fresh offset,
    since offset isn't persisted) and, if this run's OCR succeeds, replace
    the old blank record instead of duplicating it."""
    if RECORDS_PATH.exists():
        try:
            prev = json.loads(RECORDS_PATH.read_text(encoding="utf-8"))
            known_good = {r["doc_number"] for r in prev if r.get("doc_number") and (r.get("address") or r.get("owner"))}
            return known_good, prev
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

    future_rows = []
    seen_docs = set()
    offset = 0
    page = 0
    prev_page_was_full = False

    while True:
        if page + 1 > MAX_PAGES:
            log.warning(f"Hit MAX_PAGES={MAX_PAGES} -- stopping, rest deferred to next run")
            break
        url = f"{SEARCH_URL}&offset={offset}"
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
                    return out;
                    """,
                    row,
                ) or {}
                doc_type = (data.get("col-3") or "").strip().upper()
                recorded_date = (data.get("col-4") or "").strip()
                sale_date = (data.get("col-5") or "").strip()
                doc_number = (data.get("col-6") or "").strip()
                city = (data.get("col-8") or "").strip()

                if not doc_number or doc_number in seen_docs:
                    continue
                if "FORECLOSURE" not in doc_type:
                    continue  # skip VOID FC etc.
                sale_dt = parse_mdy(sale_date)
                if not sale_dt or sale_dt.date() < TODAY_CT.date():
                    continue  # past auction -- discard, per explicit instruction

                seen_docs.add(doc_number)
                # 2026-09-17 fix: this SPA doesn't expose a real <a href> on
                # each row (confirmed live -- querySelector('a') either
                # found nothing or a non-navigable link, so every OCR
                # fetch silently short-circuited on a falsy href and 0/50
                # docs got real address/owner data). Rows navigate via a
                # JS click handler instead. Store this row's page offset
                # so ocr_doc() can reload the exact same results page later
                # and click the real row by doc-number text match, instead
                # of trying to read a URL that doesn't reliably exist.
                future_rows.append({
                    "doc_number": doc_number,
                    "recorded_date": recorded_date,
                    "sale_date": sale_date,
                    "city": city,
                    "offset": offset,
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


def ocr_doc(driver, offset, doc_number):
    """
    Reload the results page this doc_number was found on, click that exact
    row (by matching its col-6 text -- see the fix note in
    scrape_search_results for why this doesn't just navigate a stored
    href), then grab page 1's signed image URL and OCR it. Returns
    (address, owner) -- either may be "" if the template didn't match or
    OCR came back too noisy to parse.
    """
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support.ui import WebDriverWait

    # 2026-09-18: every failure branch below used log.debug(), but the
    # module logger is configured at INFO -- every one of these diagnostic
    # lines has been silently swallowed since this function was written.
    # Confirmed live: two prior "fix" attempts both shipped with 0% OCR
    # success and zero visibility into which stage was actually failing.
    # Bumped to log.info() so a real run finally shows where the pipeline
    # breaks, and a per-stage counter is returned so main() can print an
    # aggregate breakdown across the whole batch instead of just a final
    # 0/N.
    stage = "start"
    try:
        driver.set_page_load_timeout(PAGE_TIMEOUT)
        driver.get(f"{SEARCH_URL}&offset={offset}")
        WebDriverWait(driver, PAGE_TIMEOUT).until(
            lambda d: d.find_elements(By.CSS_SELECTOR, "table tbody tr")
        )
        time.sleep(1.5)
        stage = "page_loaded"

        target_cell = None
        for cell in driver.find_elements(By.CSS_SELECTOR, "td.col-6"):
            if cell.text.strip() == doc_number:
                target_cell = cell
                break
        if not target_cell:
            log.info(f"  [{doc_number}] STOP at {stage}: row not found at offset {offset} (site may have reordered)")
            return "", "", stage
        row = target_cell.find_element(By.XPATH, "..")
        stage = "row_found"

        # Not certain which element actually carries the SPA's click
        # handler -- try row, then cell, then any link inside the row,
        # each with a short (not PAGE_TIMEOUT-length) wait so a wrong
        # guess fails fast instead of burning minutes per doc across a
        # 30-doc run.
        navigated = False
        nav_errors = []
        for name, clickable in (("row", row), ("cell", target_cell)):
            try:
                clickable.click()
                WebDriverWait(driver, 15).until(lambda d: "/results" not in d.current_url)
                navigated = True
                stage = f"clicked_{name}"
                break
            except Exception as e:
                nav_errors.append(f"{name}: {type(e).__name__}: {e}")
                continue
        if not navigated:
            links = row.find_elements(By.TAG_NAME, "a")
            if links:
                try:
                    links[0].click()
                    WebDriverWait(driver, 15).until(lambda d: "/results" not in d.current_url)
                    navigated = True
                    stage = "clicked_link"
                except Exception as e:
                    nav_errors.append(f"link: {type(e).__name__}: {e}")
            else:
                nav_errors.append("no <a> tag found inside row")
        if not navigated:
            log.info(f"  [{doc_number}] STOP at {stage}: click never navigated away from /results. "
                     f"Attempts: {' | '.join(nav_errors)}. Current URL: {driver.current_url}")
            return "", "", stage
        time.sleep(2)
        log.info(f"  [{doc_number}] navigated OK ({stage}) -> {driver.current_url}")

        img = None
        for attempt in range(6):
            imgs = driver.find_elements(By.CSS_SELECTOR, "img[src*='/files/documents/']")
            if imgs:
                img = imgs[0]
                break
            time.sleep(2)
        if not img:
            # dump what images/selectors ARE on the page, once, to see what
            # the real viewer markup looks like if our selector is stale
            all_imgs = driver.find_elements(By.TAG_NAME, "img")
            srcs = [i.get_attribute("src") for i in all_imgs[:10]]
            log.info(f"  [{doc_number}] STOP at {stage}: no img[src*='/files/documents/'] found after 6 tries. "
                     f"Page has {len(all_imgs)} <img> total, first few srcs: {srcs}")
            return "", "", stage
        stage = "image_found"
        img_url = img.get_attribute("src")
        if not img_url:
            log.info(f"  [{doc_number}] STOP at {stage}: img element found but src attribute empty")
            return "", "", stage

        import tempfile
        try:
            req = urllib.request.Request(img_url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=30) as r:
                image_bytes = r.read()
        except Exception as e:
            log.info(f"  [{doc_number}] STOP at {stage}: image download failed: {type(e).__name__}: {e} "
                     f"(url={img_url[:120]})")
            return "", "", stage
        stage = "image_downloaded"
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
            tmp.write(image_bytes)
            tmp_path = tmp.name

        import pytesseract
        from PIL import Image
        try:
            text = pytesseract.image_to_string(Image.open(tmp_path))
        except Exception as e:
            log.info(f"  [{doc_number}] STOP at {stage}: pytesseract/PIL failed on downloaded image "
                     f"({len(image_bytes)} bytes): {type(e).__name__}: {e}")
            try:
                os.unlink(tmp_path)
            except Exception:
                pass
            return "", "", stage
        stage = "ocr_ran"
        try:
            os.unlink(tmp_path)
        except Exception:
            pass

        addr_match = ADDRESS_RE.search(text) or ADDRESS_FALLBACK_RE.search(text)
        address = addr_match.group(1).strip().rstrip(".") if addr_match else ""
        grantor_match = GRANTOR_RE.search(text)
        owner = grantor_match.group(1).strip().rstrip(".") if grantor_match else ""
        if not address and not owner:
            snippet = re.sub(r"\s+", " ", text).strip()[:300]
            log.info(f"  [{doc_number}] STOP at {stage}: OCR produced {len(text)} chars but neither regex "
                     f"matched. Text snippet: {snippet!r}")
        stage = "done"
        return address, owner, stage
    except Exception as e:
        log.info(f"  [{doc_number}] EXCEPTION at {stage}: {type(e).__name__}: {e}")
        return "", "", stage


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


def build_record(row, address, owner, is_new=True):
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
        "is_new": is_new,
        "doc_number": row["doc_number"],
        "date_filed": row["recorded_date"],
        "date_recorded": row["recorded_date"],
        "sale_date": row["sale_date"],
        "days_until_sale": days_until,
        "run_ts": RUN_TIMESTAMP,
        "flags": (["NEW"] if is_new else ["OCR RETRY"]) + ["HAS SALE DATE"] + (["NO ADDRESS - OCR MISS"] if not address else []) + (["NO OWNER - OCR MISS"] if not owner else []),
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
    known_good_docs, prev_records = load_known_docs()
    all_prev_doc_numbers = {r["doc_number"] for r in prev_records if r.get("doc_number")}
    blank_doc_count = len(all_prev_doc_numbers) - len(known_good_docs)
    log.info(f"Loaded {len(prev_records)} existing records ({len(known_good_docs)} with real OCR data, "
             f"{blank_doc_count} blank and eligible for retry)")

    driver = get_driver()
    try:
        future_rows = scrape_search_results(driver)
        log.info(f"Found {len(future_rows)} total future-dated FORECLOSURE notices in window")

        new_rows = [r for r in future_rows if r["doc_number"] not in known_good_docs]
        log.info(f"{len(new_rows)} are new-or-retry (not already OCR'd successfully)")

        new_records = []
        ocr_hits = 0
        stage_counts = {}
        for i, row in enumerate(new_rows[:OCR_LIMIT]):
            log.info(f"[{i + 1}/{min(len(new_rows), OCR_LIMIT)}] OCR doc {row['doc_number']} (sale {row['sale_date']})")
            address, owner, stage = ocr_doc(driver, row["offset"], row["doc_number"])
            stage_counts[stage] = stage_counts.get(stage, 0) + 1
            if address or owner:
                ocr_hits += 1
            rec = build_record(row, address, owner, is_new=(row["doc_number"] not in all_prev_doc_numbers))
            new_records.append(rec)
            time.sleep(2)
        log.info(f"OCR: {ocr_hits}/{len(new_records)} docs yielded at least an address or owner")
        log.info(f"OCR stage breakdown (where the pipeline stopped for each doc): {stage_counts}")
    finally:
        driver.quit()

    # Drop any existing record whose sale date has since passed -- belt and
    # suspenders alongside purge_past_auctions.py, since this scraper's own
    # job is specifically "future auctions only." Also drop any record this
    # run just retried (whether or not the retry itself found an address)
    # so it doesn't sit in records.json twice -- new_records already has
    # this run's version of it.
    retried_doc_numbers = {row["doc_number"] for row in new_rows[:OCR_LIMIT]}
    kept_prev = []
    for r in prev_records:
        sd = parse_mdy(r.get("sale_date", ""))
        if sd and sd.date() < TODAY_CT.date():
            continue
        if r.get("doc_number") in retried_doc_numbers:
            continue
        kept_prev.append(r)
    dropped = len(prev_records) - len(kept_prev)
    if dropped:
        log.info(f"Dropped {dropped} existing record(s) whose sale date has passed or was re-OCR'd this run")

    all_records = kept_prev + new_records
    RECORDS_PATH.parent.mkdir(parents=True, exist_ok=True)
    RECORDS_PATH.write_text(json.dumps(all_records, indent=2, ensure_ascii=False), encoding="utf-8")
    log.info(f"Saved {len(all_records)} records ({len(new_records)} new)")
    print(f"new={len(new_records)}")


if __name__ == "__main__":
    main()
