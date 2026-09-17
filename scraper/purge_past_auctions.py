"""
purge_past_auctions.py
Removes leads where sale_date (auction date) has already passed.

2026-08-28: now runs automatically as its own step in scrape.yml, every
scrape. Previously this was manual-only and, in practice, never actually
run -- 520 dead leads piled up silently before the backlog-recovery fix
surfaced them all at once as "new."

Also removes:
- NOF/TAX leads with no sale_date older than 180 days (auction passed unknown)
- Keeps: LP, VBP, CE, APPT, any lead with future sale_date, any with GHL activity
"""
import json, logging, re
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

RECORDS_PATH = Path("dashboard/records.json")
DATA_PATH    = Path("data/records.json")
# 2026-09-01: TODAY = datetime.now() used the GitHub Actions runner's clock,
# which is UTC -- Bexar auctions run on Central time, so every 09/01 lead
# got treated as "auction_passed" the instant UTC crossed midnight into
# 9/1 (7pm CDT on 8/31, still the evening BEFORE the actual auction).
# 418 leads got purged hours early, including 158 that were already
# pushed to Jarvis, because should_purge() purges auction_passed leads
# even if worked -- confirmed live via the push-tracking regression CI
# check (ghl_pushed 555 -> 397) and reverted same night. Use the actual
# Central-time date instead so this can't fire early again.
TODAY = datetime.now(ZoneInfo("America/Chicago")).replace(tzinfo=None)

def parse_date(s):
    if not s:
        return None
    s = s.strip()
    try:
        p = s.split("/")
        if len(p) == 3:
            return datetime(int(p[2]), int(p[0]), int(p[1]))
        if len(p) == 2:
            return datetime(int(p[1]), int(p[0]), 1)
    except Exception:
        pass
    try:
        return datetime.fromisoformat(s[:10])
    except Exception:
        pass
    return None

def days_old(rec):
    dt = parse_date(rec.get("date_filed") or rec.get("opened_date"))
    if not dt:
        return 0
    return (TODAY - dt).days

def has_ghl_activity(rec):
    return bool(
        rec.get("dash_phone") or
        rec.get("ghl_id") or
        rec.get("ghl_pushed") or
        rec.get("dash_dispo") not in (None, "", "new") or
        rec.get("dash_notes")
    )

def should_purge(rec):
    lead_type = rec.get("type", "")
    source    = rec.get("source", "")

    # Never purge VBP, CE, APPT — no auction/staleness date relevance
    if lead_type in ("APPT", "VBP", "CE") or source in ("vbp_ce", "code_enforcement"):
        return False, "non_foreclosure_type"

    # LP: usually converts to NOF within weeks/months. If it hasn't in
    # 180 days it's almost certainly resolved/dismissed and stale.
    if lead_type == "LP":
        if has_ghl_activity(rec):
            return False, "has_ghl_activity"
        age = days_old(rec)
        if age > 180:
            return True, f"lp_stale ({age}d old)"
        return False, "keep"

    # NOF / TAX leads
    if lead_type in ("NOF", "TAX"):
        sale_date = rec.get("sale_date", "")
        if sale_date:
            dt = parse_date(sale_date)
            # Compare calendar dates, not date-vs-datetime -- auctions run
            # late morning/afternoon Central, so a lead is only "passed"
            # once we're a full day PAST the sale date, not the instant
            # the clock crosses into the sale date itself (same bug class
            # as the UTC/Central mismatch above, just same-day instead of
            # a day early).
            if dt and dt.date() < TODAY.date():
                # Auction passed — purge even if worked in GHL, it's already
                # in Jarvis and doesn't need to also live in the dash.
                return True, f"auction_passed ({sale_date})"

        # No sale date + stale: keep if someone's actively worked it
        if has_ghl_activity(rec):
            return False, "has_ghl_activity"
        # No sale date + older than 180 days = stale, auction likely passed
        age = days_old(rec)
        if age > 180:
            return True, f"no_sale_date_stale ({age}d old)"

    return False, "keep"

def main():
    if not RECORDS_PATH.exists():
        log.error(f"records.json not found at {RECORDS_PATH}")
        return

    records = json.loads(RECORDS_PATH.read_text(encoding="utf-8"))
    log.info(f"Loaded {len(records)} records")

    keep    = []
    purged  = []
    reasons = {}

    for rec in records:
        should, reason = should_purge(rec)
        if should:
            purged.append(rec)
            reasons[reason] = reasons.get(reason, 0) + 1
        else:
            keep.append(rec)

    log.info(f"\nPurge results:")
    log.info(f"  Keeping:  {len(keep)}")
    log.info(f"  Purging:  {len(purged)}")
    for reason, count in sorted(reasons.items(), key=lambda x: -x[1]):
        log.info(f"    {reason}: {count}")

    if not purged:
        log.info("Nothing to purge — records.json unchanged")
        return

    # Log sample of purged leads
    log.info(f"\nSample purged leads:")
    for rec in purged[:10]:
        log.info(f"  {rec.get('owner','—')} | {rec.get('address','—')} | sale={rec.get('sale_date','—')} | filed={rec.get('date_filed','—')}")

    RECORDS_PATH.write_text(
        json.dumps(keep, ensure_ascii=False),
        encoding="utf-8"
    )
    if DATA_PATH.exists():
        DATA_PATH.write_text(json.dumps(keep, indent=2), encoding="utf-8")
    log.info(f"\nrecords.json saved: {len(keep)} records remaining" +
             (f" (both {RECORDS_PATH} and {DATA_PATH})" if DATA_PATH.exists() else ""))

if __name__ == "__main__":
    main()
