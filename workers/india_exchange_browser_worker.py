import csv
import io
import json
import os
import re
import sys
from datetime import datetime, timezone

import requests
from playwright.sync_api import sync_playwright

FUNCTION_URL = os.environ.get("SUPABASE_FUNCTION_URL", "https://lkqdyyqawxtjqobaqwff.supabase.co/functions/v1/pharma-universe-classifier-v1")
WORKER_TOKEN = os.environ["GPEIS_WORKER_TOKEN"]
BATCH_SIZE = min(max(int(os.environ.get("BATCH_SIZE", "5")), 1), 10)


def norm(value):
    return re.sub(r"\s+", " ", re.sub(r"[^A-Z0-9]+", " ", str(value or "").upper())).strip()


def post(payload):
    r = requests.post(FUNCTION_URL, headers={"content-type": "application/json", "x-gpeis-worker-token": WORKER_TOKEN}, json=payload, timeout=90)
    if not r.ok:
        raise RuntimeError(f"Supabase function HTTP {r.status_code}: {r.text[:1000]}")
    return r.json()


def first_values(obj, keys):
    wanted = {norm(k).replace(" ", "") for k in keys}
    found = []

    def walk(x):
        if isinstance(x, dict):
            for k, v in x.items():
                if norm(k).replace(" ", "") in wanted and isinstance(v, (str, int, float)) and v not in (None, ""):
                    found.append(str(v))
                walk(v)
        elif isinstance(x, list):
            for v in x:
                walk(v)

    walk(obj)
    return found


def identity_check(job, raw):
    metadata = job.get("metadata") or {}
    expected_ticker = norm(metadata.get("ticker") or job.get("ticker") or job.get("source_record_key"))
    expected_isin = norm(metadata.get("isin") or job.get("isin"))
    expected_names = {
        norm(job.get("legal_name")),
        norm(job.get("common_name")),
        norm((job.get("companies") or {}).get("legal_name")),
        norm((job.get("companies") or {}).get("common_name")),
    } - {""}

    tickers = {norm(x) for x in first_values(raw, ["symbol", "ticker", "scripcode", "securityCode", "SCRIP_CD", "Scrip Code"])}
    isins = {norm(x) for x in first_values(raw, ["isin", "isinCode", "ISIN", "ISIN_NUMBER", "ISIN No", "ISIN No."])}
    names = {norm(x) for x in first_values(raw, ["companyName", "company", "securityName", "issuerName", "name", "Scrip_Name", "Issuer_Name", "Security Name"])}

    ticker_ok = not expected_ticker or expected_ticker in tickers
    isin_ok = not expected_isin or expected_isin in isins
    name_ok = not expected_names or any(a == b or a in b or b in a for a in expected_names for b in names if b)
    strong = ticker_ok and (isin_ok or name_ok or (not expected_isin and not expected_names))
    return {
        "passed": bool(strong),
        "expected_ticker": expected_ticker,
        "observed_tickers": sorted(tickers),
        "expected_isin": expected_isin,
        "observed_isins": sorted(isins),
        "expected_names": sorted(expected_names),
        "observed_names": sorted(names)[:10],
        "ticker_match": ticker_ok,
        "isin_match": isin_ok,
        "name_match": name_ok,
    }


def nse_page_raw(page, ticker):
    url = f"https://www.nseindia.com/get-quotes/equity?symbol={ticker}"
    last_error = None
    for attempt in range(2):
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(2500)
            text = page.locator("body").inner_text(timeout=30000)
            title = page.title()
            break
        except Exception as exc:
            last_error = exc
            if attempt == 1:
                raise RuntimeError(f"NSE quote page failed for {ticker}: {last_error}")
            page.wait_for_timeout(3000)

    lines = [re.sub(r"\s+", " ", line).strip() for line in text.splitlines()]
    lines = [line for line in lines if line]
    basic_industry = None
    for i, line in enumerate(lines):
        if norm(line) == "BASIC INDUSTRY" and i + 1 < len(lines):
            basic_industry = lines[i + 1]
            break

    isin = None
    m = re.search(r"\b(IN[A-Z0-9]{10})\b", text.upper())
    if m:
        isin = m.group(1)

    return {
        "symbol": ticker,
        "isin": isin,
        "companyName": title,
        "basicIndustry": basic_industry,
        "page_title": title,
        "page_url": url,
        "page_text": "\n".join(lines[:1200]),
    }, url


def bse_api_raw(request, ticker):
    headers = {
        "Accept": "application/json, text/plain, */*",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/128 Safari/537.36",
        "Referer": "https://www.bseindia.com/",
    }
    url = f"https://api.bseindia.com/BseIndiaAPI/api/ListofScripData/w?scripcode={ticker}&Group=&industry=&segment=Equity&status="
    response = request.get(url, headers=headers, timeout=60000, fail_on_status_code=False)
    if not response.ok:
        raise RuntimeError(f"BSE HTTP {response.status}: {response.text()[:500]}")
    raw = response.json()
    if raw == [] or raw == {} or raw is None:
        raise RuntimeError(f"BSE returned no record for scripcode {ticker}")
    return raw, url


def bse_csv_raw(request, ticker):
    """Fetch BSE's reference-security CSV and extract this scrip's industry."""
    headers = {
        "Accept": "text/csv, text/plain, */*",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/128 Safari/537.36",
        "Referer": "https://www.bseindia.com/",
    }
    url = f"https://api.bseindia.com/BseIndiaAPI/api/LitsOfScripCSVDownload/w?segment=Equity&status=Active&industry=&Group=&Scripcode={ticker}"
    response = request.get(url, headers=headers, timeout=60000, fail_on_status_code=False)
    if not response.ok:
        raise RuntimeError(f"BSE CSV HTTP {response.status}: {response.text()[:500]}")

    text = response.text().lstrip("\ufeff")
    reader = csv.DictReader(io.StringIO(text))
    rows = list(reader)
    if not rows:
        raise RuntimeError(f"BSE CSV returned no rows for scripcode {ticker}")

    def pick(row, candidates):
        by_norm = {norm(k).replace(" ", ""): v for k, v in row.items() if k is not None}
        for key in candidates:
            value = by_norm.get(norm(key).replace(" ", ""))
            if value not in (None, ""):
                return value.strip()
        return None

    target = norm(ticker)
    matches = []
    for row in rows:
        code = pick(row, ["Scrip Code", "SCRIP_CD", "Scripcode", "Security Code"])
        if norm(code) == target:
            matches.append(row)

    if not matches:
        raise RuntimeError(f"BSE CSV returned no matching row for scripcode {ticker}")

    row = matches[0]
    industry = pick(row, ["Industry", "INDUSTRY"])
    if not industry:
        raise RuntimeError(f"BSE authoritative CSV has no industry for scripcode {ticker}")

    return {
        "SCRIP_CD": pick(row, ["Scrip Code", "SCRIP_CD", "Scripcode", "Security Code"]) or ticker,
        "ISIN_NUMBER": pick(row, ["ISIN", "ISIN_NUMBER", "ISIN No", "ISIN No."]),
        "Scrip_Name": pick(row, ["Scrip Name", "Scrip_Name", "Security Name", "Company Name"]),
        "Issuer_Name": pick(row, ["Issuer Name", "Issuer_Name"]),
        "INDUSTRY": industry,
        "raw_csv_row": row,
    }, url


def fetch_exchange(page, request, mic, ticker):
    if mic == "XNSE":
        return nse_page_raw(page, ticker)
    if mic == "XBOM":
        try:
            page.goto("https://www.bseindia.com/", wait_until="domcontentloaded", timeout=60000)
        except Exception:
            pass
        meta, meta_url = bse_api_raw(request, ticker)
        industry, industry_url = bse_csv_raw(request, ticker)
        return {"security_metadata": meta, "industry_reference": industry}, f"{meta_url} | {industry_url}"
    raise RuntimeError(f"unsupported MIC {mic}")


def extract_classification(raw, mic):
    if mic == "XNSE":
        basic = raw.get("basicIndustry") if isinstance(raw, dict) else None
        if not basic:
            raise RuntimeError("NSE quote page contained no Basic Industry classification")
        return {"basicIndustry": basic}

    if mic == "XBOM":
        ref = raw.get("industry_reference") if isinstance(raw, dict) else None
        if not isinstance(ref, dict) or not ref.get("INDUSTRY"):
            raise RuntimeError("BSE reference response contained no Industry classification")
        return {"industry": ref["INDUSTRY"]}

    return raw


def process_job(page, request, job):
    metadata = job.get("metadata") or {}
    mic = metadata.get("mic") or job.get("exchange_mic")
    ticker = str(metadata.get("ticker") or job.get("ticker") or job.get("source_record_key") or "").strip()
    if not ticker:
        raise RuntimeError("queue item has no ticker")

    raw, source_url = fetch_exchange(page, request, mic, ticker)

    if mic == "XNSE":
        identity_raw = {
            "symbol": raw.get("symbol"),
            "isin": raw.get("isin"),
            "companyName": raw.get("companyName"),
        }
    else:
        identity_raw = raw.get("security_metadata") or raw.get("industry_reference") or raw

    identity = identity_check(job, identity_raw)
    if not identity["passed"]:
        raise RuntimeError("identity validation failed: " + json.dumps(identity, separators=(",", ":")))

    classification = extract_classification(raw, mic)
    source_system = "NSE_INDUSTRY_CLASSIFICATION" if mic == "XNSE" else "BSE_INDUSTRY_CLASSIFICATION"
    return {
        "mode": "github_ingest",
        "queue_id": job["queue_id"],
        "source_system": source_system,
        "source_record_key": f"{mic}:{ticker}",
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "source_url": source_url,
        "mic": mic,
        "ticker": ticker,
        "identity_validation": identity,
        "classification": classification,
        "raw_response": raw,
    }


def main():
    claimed = post({"mode": "github_claim", "limit": BATCH_SIZE})
    jobs = claimed.get("jobs") or []
    print(json.dumps({"claim_status": claimed.get("status"), "count": len(jobs)}, indent=2))
    if not jobs:
        return 0

    resolved = failed = 0
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--disable-http2"])
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/128 Safari/537.36",
            locale="en-US",
            viewport={"width": 1440, "height": 900},
        )
        page = context.new_page()
        request = context.request
        for job in jobs:
            try:
                response = post(process_job(page, request, job))
                print(json.dumps({"queue_id": job["queue_id"], "status": response.get("status"), "direction": response.get("direction")}))
                resolved += 1
            except Exception as exc:
                failed += 1
                message = str(exc)[:3000]
                print(json.dumps({"queue_id": job.get("queue_id"), "status": "FAILED", "error": message}), file=sys.stderr)
                try:
                    post({"mode": "github_fail", "queue_id": job["queue_id"], "error": message})
                except Exception as reset_exc:
                    print(f"failed to reset queue item: {reset_exc}", file=sys.stderr)
        browser.close()

    print(json.dumps({"status": "COMPLETED", "claimed": len(jobs), "resolved": resolved, "failed": failed}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
