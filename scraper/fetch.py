"""
Dallas County Foreclosure Notice Scraper v1

Data source: dallas.tx.publicsearch.us, department=FC (Foreclosures) -- the
same Tyler PublicSearch platform Bexar uses, and (confirmed live 2026-09-17)
the exact same table column layout: col-3=doc type, col-4=recorded date,
col-5=sale date, col-6=doc number, col-8=property address. Confirmed via the
county's own site (dallascounty.org/.../foreclosures.php): notices filed on
or after 2/24/2026 live here; older ones are individual PDFs on that page,
not handled by this scraper.

The SEARCH RESULTS table itself only gives the city in col-8, not a real
street address -- but (corrected 2026-09-18, see ocr_doc()'s own docstring
for the full story) each row's DOC DETAIL page has its own SUMMARY panel
with a real structured "Property Address" field, plus a "Parties" field
that's usually but not always populated with owner name. This scraper
clicks through to that detail page and reads those fields directly out of
the DOM. OCR (pytesseract) is now only a fallback for the owner name
specifically, used when "Parties" comes back empty and the scanned notice
image is available to try instead.
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
# here.
#
# 2026-09-21: switched instrumentDateRange -> recordedDateRange. The three
# sibling scrapers (nueces/bexar/wilson-leads) already found and fixed this
# exact bug on 2026-08-28 -- instrumentDateRange "started returning
# inconsistent/incomplete results" for a department-scoped FC search on this
# same PublicSearch platform -- but the fix was never ported here. Live
# symptom matched exactly: "Found 0 total future-dated FORECLOSURE notices"
# despite the site visibly having current listings, right after the
# DALLAS_CLERK_EMAIL/PASSWORD login fix was confirmed working (login was not
# the cause). recordedDateRange's end must not extend into the future (recorded
# dates are never forward-dated, unlike sale dates), so no more +220d padding.
SEARCH_START = "20260201"
SEARCH_END   = TODAY_CT.strftime("%Y%m%d")

PAGE_TIMEOUT = 120
MAX_PAGES    = 60          # 60*50 = 3000 rows -- comfortably above the current ~2,501 total
OCR_LIMIT    = 6           # 2026-09-18: confirmed live the site rate-limits/blocks the runner's IP
                            # after the very first doc-detail page load -- 29 of 30 docs in a full
                            # run hit TimeoutException at the *next* page load, every single one,
                            # right at PAGE_TIMEOUT. The extraction itself is fixed and confirmed
                            # working (doc #1 came back clean in ~14s), this cap is purely about not
                            # tripping the site's rate limiter. Backlog carries over run to run via
                            # the retry-eligible logic in load_known_docs()/main(), and the workflow
                            # runs twice daily, so a ~50-doc backlog clears in about a week either way.
                            # Each OCR fetch reloads a full results page to reach a clickable row
                            # (see ocr_doc below), so this is deliberately conservative -- 2026-09-17
                            # test run's rapid page loads got a real "request timed out" from the
                            # county's own site under load; don't hammer it harder than this.

# On-market checking (ported from nueces-leads 2026-09-25) -- flags leads
# already listed for sale/rent elsewhere via homeharvest (pip, MIT license)
# against Realtor.com's public page data, no API key/cost.
ON_MARKET_STATUSES      = {"FOR_SALE", "PENDING"}
ON_MARKET_FETCH_LIMIT   = 20   # max never-checked leads to look up per run
ON_MARKET_REFRESH_DAYS  = 7    # re-check a lead's market status at most this often
ON_MARKET_REFRESH_LIMIT = 10   # max already-checked leads to re-check per run

SEARCH_URL = (
    f"{PUBLICSEARCH_BASE}/results"
    f"?department=FC"
    f"&recordedDateRange={SEARCH_START}%2C{SEARCH_END}"
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


def login_publicsearch(driver):
    """
    Ported from bexar-leads's fetch.py 2026-09-18, in response to a real
    HTTP 401 on the signed document-image URL (confirmed live: address
    extraction from the SUMMARY panel works anonymously, but downloading
    the actual page image does not). Bexar's own scraper has always
    logged in for this exact reason and has never hit Dallas's other
    open mystery either -- every doc after the first one in a run timing
    out at the page-load step, regardless of added delay. Anonymous
    Tyler PublicSearch sessions plausibly get rate-limited/blocked far
    more aggressively than authenticated ones; this single fix may
    resolve both problems at once, but needs its own DALLAS_CLERK_EMAIL/
    DALLAS_CLERK_PASSWORD secret -- a Tyler PublicSearch login is scoped
    to one county subdomain, so Bexar's existing CLERK_EMAIL/PASSWORD
    account will not carry over here. No login attempted at all if those
    aren't set (was true of this scraper's entire history until now).
    """
    from selenium.webdriver.common.by import By

    email    = os.environ.get("DALLAS_CLERK_EMAIL", "")
    password = os.environ.get("DALLAS_CLERK_PASSWORD", "")
    if not email or not password:
        log.warning("No DALLAS_CLERK_EMAIL/DALLAS_CLERK_PASSWORD — skipping login, "
                     "running anonymous (address-only, no OCR image access)")
        return False
    try:
        driver.set_page_load_timeout(20)
        driver.get(f"{PUBLICSEARCH_BASE}/signin")
        time.sleep(4)
        log.info(f"Login page title: {driver.title} | url: {driver.current_url}")

        email_el = None
        for sel in ["input[type='email']", "input[name='email']",
                    "input[name='username']", "input[placeholder*='mail']",
                    "input[placeholder*='ser']"]:
            try:
                els = driver.find_elements(By.CSS_SELECTOR, sel)
                if els:
                    email_el = els[0]
                    break
            except Exception:
                pass
        if not email_el:
            log.warning("Login: email field not found")
            return False
        email_el.clear()
        email_el.send_keys(email)

        pass_el = None
        for sel in ["input[type='password']", "input[name='password']"]:
            try:
                els = driver.find_elements(By.CSS_SELECTOR, sel)
                if els:
                    pass_el = els[0]
                    break
            except Exception:
                pass
        if not pass_el:
            log.warning("Login: password field not found")
            return False
        pass_el.clear()
        pass_el.send_keys(password)

        submitted = False
        for sel in ["button[type='submit']", "input[type='submit']", "button"]:
            try:
                btns = driver.find_elements(By.CSS_SELECTOR, sel)
                for btn in btns:
                    txt = (btn.text or "").lower()
                    if any(x in txt for x in ["sign in", "login", "log in", "submit", ""]):
                        btn.click()
                        submitted = True
                        break
            except Exception:
                pass
            if submitted:
                break
        if not submitted:
            pass_el.submit()

        time.sleep(4)
        if "login" not in driver.current_url.lower() and "signin" not in driver.current_url.lower():
            log.info("PublicSearch login OK")
            return True
        log.warning(f"Login: still on signin page after submit (url={driver.current_url})")
        return False
    except Exception as e:
        log.warning(f"PublicSearch login error: {type(e).__name__}: {e}")
        return False


def parse_mdy(s):
    if not s:
        return None
    try:
        return datetime.strptime(s.strip(), "%m/%d/%Y")
    except Exception:
        return None


def load_known_docs():
    """Returns (known_good_docs, prev_records). known_good_docs is doc
    numbers that already have BOTH a real address and owner -- genuinely
    done, skip forever. Records missing either (every doc from this
    scraper's whole two-prior-attempt 0%-OCR history) are deliberately left
    OUT of that set: confirmed live 2026-09-18 that treating "already has a
    row in records.json" as "done" meant a fresh run found 0 new docs and
    never retried a single one of the 79 already sitting there with blank
    OCR data. Those get re-fetched (using the current run's fresh offset,
    since offset isn't persisted) and, if this run's OCR succeeds, replace
    the old blank record instead of duplicating it.

    2026-09-21: this used to be "address OR owner" -- address (SUMMARY
    panel) and owner (separate OCR-image fallback, added later) are
    independently sourced, so a doc that got an address before the owner
    regexes existed was marked "done" forever with owner permanently
    blank. Confirmed live: 0/79 owners after two full runs with working
    grantor patterns, because every doc that had picked up an address in
    an earlier run was never retried for the owner it was still missing."""
    if RECORDS_PATH.exists():
        try:
            prev = json.loads(RECORDS_PATH.read_text(encoding="utf-8"))
            known_good = {r["doc_number"] for r in prev if r.get("doc_number") and r.get("address") and r.get("owner")}
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


# 2026-09-18: original pattern assumed "Grantor: NAME" -- real notice text
# (confirmed reading an actual doc) puts the name BEFORE the word grantor,
# e.g. "with PATRICE M BOYD A SINGLE WOMAN, grantor(s) and MORTGAGE
# ELECTRONIC..." and separately "executed by PATRICE M BOYD A SINGLE
# WOMAN, securing the payment of...". Try both; the "executed by ...
# securing" phrasing is the more reliable of the two (matches the same
# phrasing Bexar's own OCR regex targets).
GRANTOR_EXECUTED_RE = re.compile(
    r"executed\s+by\s+([A-Z][A-Za-z0-9 .,&'\-]{3,80}?),?\s+securing",
    re.IGNORECASE,
)
GRANTOR_WITH_RE = re.compile(
    r"with\s+([A-Z][A-Za-z0-9 .,&'\-]{3,80}?),?\s+grantor\(?s?\)?",
    re.IGNORECASE,
)
# 2026-09-21: confirmed live on doc 202600002552 -- this county's actual
# "Notice of Trustee's Foreclosure Sale" template reads "...granted by the
# Deed of Trust executed by VINCENT DAVIS JR, A SINGLE MAN." with a period
# ending the sentence right after the name, then a new numbered paragraph
# ("6. Obligations Secured...") -- no "securing" and no "grantor(s)" appear
# anywhere near the name, so neither existing pattern matched on ANY of the
# 4 non-empty-Parties docs in that day's run (0/79 owners populated despite
# the address-OCR pipeline itself working). Match up to the terminating
# period instead.
GRANTOR_EXECUTED_PERIOD_RE = re.compile(
    r"executed\s+by\s+([A-Z][A-Za-z0-9 .,&'\-]{3,80}?)\.\s",
)
# 2026-09-21: a second, entirely different template -- confirmed live on doc
# 202600002544 -- uses a label/value table ("Trustor(s): MERRITT CROSSINGS
# LLC, A TEXAS LIMITED LIABILITY COMPANY AND ADHAAF AMER DARDAR   Original
# Beneficiary: ...") instead of prose. Dallas notices are filed by multiple
# different trustee/posting companies with their own templates -- these four
# patterns cover the ones seen so far, not necessarily every one that exists.
GRANTOR_TRUSTOR_RE = re.compile(
    r"Trustor\(?s?\)?:?\s*([A-Z][A-Za-z0-9 .,&'\-]{3,100}?)\s+Original",
    re.IGNORECASE,
)
# 2026-09-21: a fifth template (doc 202600002546) -- "WHEREAS, on June 11,
# 2025, ESPBINVESTMENTS, LLC ("Mortgagors", whether one or more), executed
# that certain deed of trust...". Confirmed via the new unmatched-OCR log
# line rather than manual in-browser reading -- exactly what that logging
# was added for.
GRANTOR_WHEREAS_RE = re.compile(
    r"WHEREAS,?\s+on\s+[A-Za-z]+\.?\s+\d{1,2},?\s+\d{4},?\s+"
    r"([A-Z][A-Za-z0-9 .,&'\-]{3,100}?)\s*\(.{0,20}?Mortgagors?",
    re.IGNORECASE,
)
# 2026-09-21: tried a bare "Grantor:" label pattern here (doc 202600002547)
# but it matched garbage live -- "BY POLUNSKY" (Polunsky Beitel Green PLLC
# is the foreclosure law firm on these notices, not the owner) instead of
# an actual name. A wrong name is worse than a blank one -- it can get used
# to contact the wrong party. Removed rather than tightened; this doc's
# actual "Grantor:" line/value needs to be read directly, not guessed at
# via a generic label match.


def ocr_doc(driver, offset, doc_number):
    """
    Reload the results page this doc_number was found on, click that exact
    row (by matching its col-6 text -- see the fix note in
    scrape_search_results for why this doesn't just navigate a stored
    href), then read the doc detail page. Returns (address, owner, stage).

    2026-09-18 correction of this module's original assumption (see the
    file docstring): the SEARCH RESULTS table's own col-8 really is just
    the city, but the individual DOC DETAIL page a row's click leads to
    has a real structured "Property Address" field in its own SUMMARY
    panel (<h3>Property Address</h3> followed by a
    .doc-preview-group__summary-group-label span) -- confirmed live
    against a real doc, address came back clean with zero OCR involved.
    This is why every previous attempt got 0%: the code was waiting on
    and requiring an `img[src*='/files/documents/']` that either doesn't
    exist on every doc or simply wasn't the right thing to wait for --
    either way the real, reliable data was sitting in the DOM the whole
    time. OCR is now only a fallback for the owner name specifically,
    used when the page's own "Parties" section says "No parties found"
    (also confirmed real and common -- not every notice's Parties data is
    populated).
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

        # Wait for the SUMMARY panel's own structured fields to render,
        # not for an image -- the panel is what actually has reliable
        # data (see this function's docstring). Poll for either the
        # "Property Address" h3 or "Parties" h3 to show up.
        summary_ready = False
        for attempt in range(8):
            h3_texts = [h.text.strip() for h in driver.find_elements(By.TAG_NAME, "h3")]
            if any(t in ("Property Address", "Parties") for t in h3_texts):
                summary_ready = True
                break
            time.sleep(1.5)
        if not summary_ready:
            log.info(f"  [{doc_number}] STOP at {stage}: SUMMARY panel never rendered "
                     f"(no 'Property Address'/'Parties' h3 after 8 tries)")
            return "", "", stage
        stage = "summary_rendered"

        address, owner = "", ""
        try:
            h3_elements = driver.find_elements(By.TAG_NAME, "h3")
            for h3 in h3_elements:
                label = h3.text.strip()
                if label not in ("Property Address", "Parties"):
                    continue
                container = h3.find_element(By.XPATH, "..")
                value_spans = container.find_elements(By.CSS_SELECTOR, ".doc-preview-group__summary-group-label")
                values = [v.text.strip() for v in value_spans if v.text.strip()]
                if label == "Property Address" and values:
                    address = values[0]
                elif label == "Parties" and values:
                    # "Parties" can list multiple grantor/grantee lines;
                    # take the first non-empty one as a best-effort owner
                    # name rather than trying to disambiguate grantor vs
                    # grantee roles from this compact view.
                    owner = values[0]
        except Exception as e:
            log.info(f"  [{doc_number}] WARN: error reading SUMMARY panel fields: {type(e).__name__}: {e}")
        stage = "summary_extracted"

        if address:
            log.info(f"  [{doc_number}] address from SUMMARY panel: {address!r}")
        if owner:
            log.info(f"  [{doc_number}] owner from SUMMARY panel: {owner!r}")

        if not owner:
            # OCR fallback for owner name only -- "Parties" came back
            # empty (confirmed a real, common case, not a bug), and the
            # scanned notice text itself (Grantor(s) line) is the only
            # remaining source for it. Address is NOT re-attempted via
            # OCR since the SUMMARY panel is the authoritative source for
            # it; if that came back blank the image likely won't do
            # better and isn't worth the extra minute-plus per doc.
            # 2026-09-18: confirmed live via direct DOM inspection -- the
            # document viewer renders the page as an SVG <image> element
            # (href/xlink:href), NOT a plain <img src=...>. This selector
            # could never match, at any wait length, regardless of how
            # long the page had to load -- the "no OCR fallback image
            # available" log line on every single prior attempt (including
            # the one doc that got past the click/timeout issue) was this
            # bug, not a real absence of an image. The image itself is
            # real and the grantor name is genuinely printed in it
            # (confirmed reading "PATRICE M BOYD A SINGLE WOMAN" directly
            # off a real notice this way).
            img = None
            for _ in range(4):
                imgs = driver.find_elements(By.CSS_SELECTOR, "svg image")
                if imgs:
                    img = imgs[0]
                    break
                time.sleep(2)
            img_url = None
            if img:
                img_url = img.get_attribute("href") or img.get_attribute("xlink:href")
            if img_url:
                stage = "image_found"
                import tempfile
                try:
                    # 2026-09-21: this urllib request is a separate HTTP
                    # client from Selenium's browser session -- it shares
                    # NO cookies with it by default, so even a successful
                    # driver-side login never touched this 401. Log in via
                    # the driver ONLY right here (never during search/
                    # listing navigation -- see main()'s note on why),
                    # then hand its session cookies to urllib explicitly.
                    # Doc-detail pages themselves load fine anonymously
                    # (confirmed above -- SUMMARY panel already read before
                    # reaching here), so this is the one place auth is
                    # actually required.
                    login_publicsearch(driver)
                    cookie_header = "; ".join(
                        f"{c['name']}={c['value']}" for c in driver.get_cookies()
                    )
                    req = urllib.request.Request(
                        img_url,
                        headers={"User-Agent": "Mozilla/5.0", "Cookie": cookie_header},
                    )
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
                    grantor_match = (GRANTOR_EXECUTED_RE.search(text) or GRANTOR_WITH_RE.search(text)
                                      or GRANTOR_EXECUTED_PERIOD_RE.search(text)
                                      or GRANTOR_TRUSTOR_RE.search(text)
                                      or GRANTOR_WHEREAS_RE.search(text))
                    if grantor_match:
                        owner = grantor_match.group(1).strip().rstrip(".")
                        log.info(f"  [{doc_number}] owner from OCR fallback: {owner!r}")
                    else:
                        # 2026-09-21: this used to be silent -- no log line at
                        # all -- which is exactly how the grantor regexes
                        # matching 0/79 owners for two runs straight went
                        # undiagnosed until read manually in-browser. The
                        # first 300 chars alone (original version of this log
                        # line) is almost always just the boilerplate military-
                        # rights disclaimer every template opens with, not the
                        # actual grantor wording -- print whichever lines
                        # contain a likely keyword instead, so a new
                        # template's real phrasing is visible directly from
                        # the log without another round of manual doc-reading.
                        kw_re = re.compile(
                            r"trustor|mortgagor|grantor|executed by|whereas|obligor|borrower",
                            re.IGNORECASE,
                        )
                        hit_lines = [ln.strip() for ln in text.splitlines() if kw_re.search(ln)]
                        if hit_lines:
                            diag = f"keyword lines: {hit_lines}"
                        else:
                            diag = f"(no keyword lines -- first 300 chars: {text[:300]!r})"
                        log.info(f"  [{doc_number}] OCR'd image but no grantor pattern matched. {diag}")
                except Exception as e:
                    log.info(f"  [{doc_number}] OCR owner-fallback failed (non-fatal): {type(e).__name__}: {e}")
                finally:
                    # Sign back out immediately -- the NEXT doc's ocr_doc()
                    # call starts by reloading the search listing, which
                    # breaks (see main()'s note) if the driver is still
                    # authenticated. A plain GET is enough; no form/submit
                    # needed to sign out.
                    try:
                        driver.get(f"{PUBLICSEARCH_BASE}/signout")
                        time.sleep(1)
                    except Exception:
                        pass
            else:
                log.info(f"  [{doc_number}] no OCR fallback image available for owner either")

        if not address and not owner:
            log.info(f"  [{doc_number}] STOP at {stage}: neither Property Address nor Parties nor OCR "
                     f"fallback yielded anything")
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


# ── On-market status (ported from nueces-leads 2026-09-25) ────────────────────
_OM_STREET_SUFFIX_WORDS = {
    "ST", "STREET", "DR", "DRIVE", "RD", "ROAD", "AVE", "AVENUE", "LN", "LANE",
    "CT", "COURT", "BLVD", "BOULEVARD", "WAY", "CIR", "CIRCLE", "TRL", "TRAIL",
    "PL", "PLACE", "PKWY", "PARKWAY", "LOOP", "RUN", "PASS", "XING", "CROSSING",
    "COVE", "BND", "BEND", "VW", "VIEW", "HOLW", "HOLLOW", "RDG", "RIDGE",
    "MDW", "MDWS", "MEADOW", "MEADOWS", "GLN", "GLEN", "HL", "HILL", "HLS",
    "HILLS", "PT", "POINT", "SQ", "SQUARE", "TER", "TERRACE", "WALK", "GRV",
    "GROVE", "VLY", "VALLEY", "N", "S", "E", "W", "NE", "NW", "SE", "SW",
}


def _om_street_core_tokens(street):
    street = str(street) if street is not None else ""
    if street in ("nan", "<NA>", "None"):
        street = ""
    s = re.sub(r"[^A-Z0-9 ]", " ", street.upper())
    return [t for t in s.split() if t not in _OM_STREET_SUFFIX_WORDS]


def _om_address_matches(searched_addr, searched_zip, row_street, row_zip):
    """Verify a HomeHarvest/Realtor.com search result actually corresponds to
    the property searched for -- confirmed live (bexar-leads, 2026-09-22)
    that scrape_property() silently returns its best guess even when
    nothing real matches. Require the house number to match exactly, at
    least one distinctive street-name word to overlap, and zip to match
    when both sides have one."""
    searched_tokens = _om_street_core_tokens(searched_addr)
    row_tokens = _om_street_core_tokens(row_street)
    if not searched_tokens or not row_tokens:
        return False
    searched_num = searched_tokens[0] if searched_tokens[0].isdigit() else None
    row_num = row_tokens[0] if row_tokens[0].isdigit() else None
    if not searched_num or searched_num != row_num:
        return False
    if not (set(searched_tokens[1:]) & set(row_tokens[1:])):
        return False
    sz = str(searched_zip) if searched_zip is not None else ""
    rz = str(row_zip) if row_zip is not None else ""
    sz = "" if sz in ("nan", "<NA>", "None") else sz.strip()[:5]
    rz = "" if rz in ("nan", "<NA>", "None") else rz.strip()[:5]
    if sz and rz and sz != rz:
        return False
    return True


def _om_first_matching_row(df, searched_addr, searched_zip):
    for _, row in df.iterrows():
        if _om_address_matches(searched_addr, searched_zip, row.get("street"), row.get("zip_code")):
            return row
    return None


def fetch_on_market_status(records):
    """
    Flags leads already listed for sale/rent elsewhere via homeharvest
    (pip, MIT license) against Realtor.com's public page data -- no API
    key, no cost. Soft dependency: any failure (network, no match, library
    error) just leaves on_market unset for that lead rather than breaking
    the run. Two passes: never-checked leads first (ON_MARKET_FETCH_LIMIT),
    then a refresh of already-checked leads older than
    ON_MARKET_REFRESH_DAYS (ON_MARKET_REFRESH_LIMIT).
    """
    import pandas as pd
    from homeharvest import scrape_property

    def clean(val):
        if val is None or pd.isna(val):
            return None
        s = str(val).strip()
        return None if s in ("", "nan", "<NA>", "None") else val

    cutoff = datetime.now(ZoneInfo("UTC")) - timedelta(days=ON_MARKET_REFRESH_DAYS)

    def is_real_address(addr):
        # Guards against a lead whose "address" field fell back to just a
        # city name (build_record() does this when OCR never found a real
        # address) -- searching that as a street address would return
        # garbage matches.
        return bool(addr) and addr.strip()[:1].isdigit()

    def needs_check(r):
        if not is_real_address(r.get("address")):
            return False
        checked_at = r.get("on_market_checked_at")
        if not checked_at:
            return True
        try:
            parsed = datetime.strptime(checked_at, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=ZoneInfo("UTC"))
            return parsed < cutoff
        except Exception:
            return True

    never_checked = [r for r in records if is_real_address(r.get("address")) and not r.get("on_market_checked_at")]
    stale_checked = [r for r in records if is_real_address(r.get("address")) and r.get("on_market_checked_at") and needs_check(r)]

    candidates = never_checked[:ON_MARKET_FETCH_LIMIT] + stale_checked[:ON_MARKET_REFRESH_LIMIT]

    if not candidates:
        log.info("On-market: no eligible leads — skipping")
        return records

    log.info(f"On-market: {len(never_checked[:ON_MARKET_FETCH_LIMIT])} new + "
             f"{len(stale_checked[:ON_MARKET_REFRESH_LIMIT])} refresh "
             f"(caps={ON_MARKET_FETCH_LIMIT}/{ON_MARKET_REFRESH_LIMIT})")
    changed = 0
    errors = 0

    for rec in candidates:
        full_addr = f"{rec['address']}, {rec.get('city', '')}, TX {rec.get('zip', '')}".strip(", ")
        try:
            df = scrape_property(location=full_addr, listing_type=["for_sale", "pending"])
            now_iso = datetime.now(ZoneInfo("UTC")).strftime("%Y-%m-%dT%H:%M:%SZ")
            was_on_market = bool(rec.get("on_market"))

            if df is None or len(df) == 0:
                rec["on_market_checked_at"] = now_iso
                continue

            row = _om_first_matching_row(df, rec["address"], rec.get("zip"))
            if row is None:
                log.info(f"  On-market [{rec.get('doc_number')}] {full_addr}: "
                         f"{len(df)} result(s) returned but none verified against this address -- treating as no match")
                rec["on_market_checked_at"] = now_iso
                continue

            status = clean(row.get("status")) or ""
            rec["on_market"] = status in ON_MARKET_STATUSES
            rec["on_market_status"] = status
            rec["on_market_checked_at"] = now_iso

            if rec["on_market"] != was_on_market:
                changed += 1
                log.info(f"  On-market [{rec.get('doc_number')}] {full_addr}: "
                         f"{was_on_market} -> {rec['on_market']} (status={status})")
        except Exception as e:
            log.warning(f"  On-market [{rec.get('doc_number')}] {full_addr}: error: {e}")
            errors += 1
        finally:
            time.sleep(1)

    log.info(f"On-market: {changed} status changes, {errors} errors out of {len(candidates)} candidates")
    return records


def main():
    known_good_docs, prev_records = load_known_docs()
    all_prev_doc_numbers = {r["doc_number"] for r in prev_records if r.get("doc_number")}
    blank_doc_count = len(all_prev_doc_numbers) - len(known_good_docs)
    log.info(f"Loaded {len(prev_records)} existing records ({len(known_good_docs)} with real OCR data, "
             f"{blank_doc_count} blank and eligible for retry)")

    driver = get_driver()
    try:
        # 2026-09-21: login must NOT be active during any listing/search
        # page load. Live-confirmed by directly reproducing in a browser:
        # the exact same department=FC/recordedDateRange search URL returns
        # 2,578 real results logged OUT, and "No Results Found" for the
        # identical URL logged IN. Some quirk on Dallas's own PublicSearch
        # instance where an authenticated session breaks this particular
        # search. ocr_doc() below reloads this same search URL per-doc (to
        # find and click the row), so login can't just happen once upfront
        # either -- it has to be scoped tightly around only the per-doc
        # image fetch, inside ocr_doc() itself. See that function for why.
        future_rows = scrape_search_results(driver)
        log.info(f"Found {len(future_rows)} total future-dated FORECLOSURE notices in window")

        new_rows = [r for r in future_rows if r["doc_number"] not in known_good_docs]
        # 2026-09-21: OCR_LIMIT is a hard rate-limit ceiling, not a runtime
        # cap (raising it risks the runner's IP getting blocked -- see
        # OCR_LIMIT's own comment), so with the backlog regularly exceeding
        # it the only lever is which docs get the 6 slots each run. These
        # were being sliced in whatever order scrape_search_results()
        # returned them, with no regard for which sale was soonest. Sort by
        # sale_date first so the docs closest to auction reliably get their
        # owner/address filled in before the backlog does.
        new_rows.sort(key=lambda r: parse_mdy(r.get("sale_date", "")) or datetime.max)
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
            time.sleep(15)  # see OCR_LIMIT's comment -- extra spacing to avoid the rate limiter
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

    try:
        all_records = fetch_on_market_status(all_records)
    except Exception as e:
        log.warning(f"On-market status error: {e}")

    RECORDS_PATH.parent.mkdir(parents=True, exist_ok=True)
    RECORDS_PATH.write_text(json.dumps(all_records, indent=2, ensure_ascii=False), encoding="utf-8")
    log.info(f"Saved {len(all_records)} records ({len(new_records)} new)")
    print(f"new={len(new_records)}")


if __name__ == "__main__":
    main()
