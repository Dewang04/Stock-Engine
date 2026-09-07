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
    r = requests.post(
        FUNCTION_URL,
        headers={"content-type": "application/json", "x-gpeis-worker-token": WORKER_TOKEN},
        json=payload,
        timeout=90,
    )
    if not r.ok:
        raise RuntimeError(f"Supabase function HTTP {r.status_code}: {r.text[:1000]}")
    return r.json()


def first_values(obj, keys):
    wanted = {norm(k).replace(" ", "") for k in keys}
    found = []
    def walk(x):
        if isinstance(x, dict):
            for k, v in x.items():
                nk = norm(k).replace(" ", "")
                if nk in wanted and isinstance(v, (str, int, float)) and v not in (None, ""):
                    found.append(str(v))
                walk(v)
        elif isinstance(x, list):
            for v in x:
                walk(v)
    walk(obj)
    return found


def identity_check(job, raw):
    metadata = job.get("metadata") or {}
    expected_ticker = norm(metadata.get("ticker"))
    expected_isin = norm(metadata.get("isin"))
    company = job.get("companies") or {}
    expected_names = {norm(company.get("legal_name")), norm(company.get("common_name"))} - {""}

    tickers = {norm(x) for x in first_values(raw, ["symbol", "ticker", "scripcode", "securityCode", "Symbol"])}
    isins = {norm(x) for x in first_values(raw, ["isin", "isinCode", "ISIN"])}
    names = {norm(x) for x in first_values(raw, ["companyName", "company", "securityName", "issuerName", "name"])}

    ticker_ok = not expected_ticker or expected_ticker in tickers
    isin_ok = not expected_isin or expected_isin in isins
    name_ok = not expected_names or any(a == b or a in b or b in a for a in expected_names for b in names if b)

    # Identity before intelligence: require at least one strong identifier.
    strong = ticker_ok and (isin_ok or name_ok or not expected_isin and not expected_names)
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


def fetch_exchange(page, mic, ticker):
    ticker_q = ticker.replace("'", "\\'")
    if mic == "XNSE":
        page.goto("https://www.nseindia.com/", wait_until="domcontentloaded", timeout=60000)
        result = page.evaluate("""async (symbol) => {
            const r = await fetch('/api/quote-equity?symbol=' + encodeURIComponent(symbol), {
                credentials: 'include', headers: {'Accept':'application/json'}
            });
            return {status:r.status, text:await r.text()};
        }""", ticker)
        if result["status"] != 200:
            raise RuntimeError(f"NSE HTTP {result['status']}: {result['text'][:500]}")
        return json.loads(result["text"]), f"https://www.nseindia.com/api/quote-equity?symbol={ticker_q}"

    if mic == "XBOM":
        page.goto("https://www.bseindia.com/", wait_until="domcontentloaded", timeout=60000)
        result = page.evaluate("""async (scrip) => {
            const u = 'https://api.bseindia.com/BseIndiaAPI/api/ListofScripData/w?scripcode=' + encodeURIComponent(scrip) + '&Group=A&industry=&segment=Equity&status=Active';
            const r = await fetch(u, {credentials:'include', headers:{'Accept':'application/json','Referer':'https://www.bseindia.com/'}});
            return {status:r.status, text:await r.text()};
        }""", ticker)
        if result["status"] != 200:
            raise RuntimeError(f"BSE HTTP {result['status']}: {result['text'][:500]}")
        return json.loads(result["text"]), f"https://api.bseindia.com/BseIndiaAPI/api/ListofScripData/w?scripcode={ticker_q}&Group=A&industry=&segment=Equity&status=Active"

    raise RuntimeError(f"unsupported MIC {mic}")


def extract_classification(raw):
    if isinstance(raw, dict):
        # Preserve the authoritative exchange structure; do not reduce it to a score.
        candidates = []
        for key in ("industryInfo", "industry", "Industry", "basicIndustry", "BasicIndustry", "sector", "Sector", "Table", "table"):
            if key in raw:
                candidates.append({key: raw[key]})
        return candidates[0] if len(candidates) == 1 else (raw if raw else {})
    if isinstance(raw, list):
        return raw[0] if raw else {}
    return raw


def process_job(page, job):
    metadata = job.get("metadata") or {}
    mic = metadata.get("mic")
    ticker = str(metadata.get("ticker") or job.get("source_record_key") or "").strip()
    if not ticker:
        raise RuntimeError("queue item has no ticker")

    raw, source_url = fetch_exchange(page, mic, ticker)
    identity = identity_check(job, raw)
    if not identity["passed"]:
        raise RuntimeError("identity validation failed: " + json.dumps(identity, separators=(",", ":")))

    classification = extract_classification(raw)
    if not classification:
        raise RuntimeError("authoritative response contained no classification payload")

    source_system = "NSE_INDICES_INDUSTRY_CLASSIFICATION" if mic == "XNSE" else "BSE_INDUSTRY_CLASSIFICATION"
    observed_at = datetime.now(timezone.utc).isoformat()
    return {
        "mode": "github_ingest",
        "queue_id": job["queue_id"],
        "source_system": source_system,
        "source_record_key": f"{mic}:{ticker}",
        "observed_at": observed_at,
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

    resolved = 0
    failed = 0
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/128 Safari/537.36",
            locale="en-US",
            viewport={"width": 1440, "height": 900},
        )
        page = context.new_page()
        for job in jobs:
            try:
                result = process_job(page, job)
                response = post(result)
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
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
