import json
import os
import sys
import time
from difflib import SequenceMatcher

import requests

NSE_CASES = [
    {"symbol": "CONCORDBIO", "isin": "INE338H01029"},
    {"symbol": "AARTIPHARM", "isin": None},
]
BSE_CASES = ["539997", "524500", "532989"]

NSE_HOME = "https://www.nseindia.com/"
NSE_API = "https://www.nseindia.com/api/quote-equity"
BSE_MOBILE = "https://m.bseindia.com/StockReach.aspx?scripcode={}"

UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
)


def safe_json(response):
    try:
        return response.json()
    except Exception:
        return None


def nse_smoke(case):
    symbol = case["symbol"]
    expected_isin = case.get("isin")
    session = requests.Session()
    session.headers.update({
        "User-Agent": UA,
        "Accept": "application/json,text/plain,*/*",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": NSE_HOME,
        "Connection": "keep-alive",
    })
    started = time.monotonic()
    result = {"exchange": "XNSE", "symbol": symbol}
    try:
        home = session.get(NSE_HOME, timeout=20)
        result["session_http_status"] = home.status_code
        result["session_cookies"] = sorted(session.cookies.keys())
        if home.status_code >= 400:
            result["error"] = f"session_http_{home.status_code}"
            return result

        response = session.get(NSE_API, params={"symbol": symbol}, timeout=20)
        result["api_http_status"] = response.status_code
        result["api_content_type"] = response.headers.get("content-type")
        data = safe_json(response)
        if not isinstance(data, dict):
            result["error"] = "api_non_json"
            result["response_preview"] = response.text[:500]
            return result

        info = data.get("info") or {}
        industry = data.get("industryInfo") or {}
        returned_isin = info.get("isin")
        result["returned_identity"] = {
            "symbol": info.get("symbol"),
            "companyName": info.get("companyName"),
            "isin": returned_isin,
        }
        result["industryInfo"] = industry
        result["isin_match"] = (
            None if not expected_isin or not returned_isin
            else returned_isin.upper() == expected_isin.upper()
        )
        result["classification_present"] = any(
            bool(industry.get(k))
            for k in ("macro", "sector", "industry", "basicIndustry")
        )
        # Preserve the raw response for smoke-test auditability without logging secrets.
        result["raw_response_keys"] = sorted(data.keys())
        if expected_isin and returned_isin and returned_isin.upper() != expected_isin.upper():
            result["error"] = "identity_mismatch_isin"
    except requests.RequestException as exc:
        result["error"] = f"request_error:{type(exc).__name__}:{exc}"
    finally:
        result["elapsed_ms"] = round((time.monotonic() - started) * 1000)
        session.close()
    return result


def extract_bse_fields(html):
    # Mobile quote is HTML. Keep extraction deliberately conservative: first look
    # for labelled fields rather than guessing a value from arbitrary page text.
    from html import unescape
    import re

    text = unescape(re.sub(r"<[^>]+>", " ", html))
    text = re.sub(r"\s+", " ", text).strip()

    def labelled(label):
        m = re.search(rf"{re.escape(label)}\s*[:\-]?\s*([^|;]+?)(?=\s+(?:Security Code|ISIN|Industry|Scrip Name|Company Name)\b|$)", text, re.I)
        return m.group(1).strip() if m else None

    return {
        "security_code": labelled("Security Code"),
        "isin": labelled("ISIN"),
        "industry": labelled("Industry"),
        "scrip_name": labelled("Scrip Name") or labelled("Company Name"),
        "text_length": len(text),
    }


def bse_smoke(scrip_code):
    started = time.monotonic()
    result = {"exchange": "XBOM", "scrip_code": scrip_code}
    try:
        headers = {
            "User-Agent": UA,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Referer": "https://m.bseindia.com/",
        }
        response = requests.get(BSE_MOBILE.format(scrip_code), headers=headers, timeout=20)
        result["http_status"] = response.status_code
        result["content_type"] = response.headers.get("content-type")
        if response.status_code >= 400:
            result["error"] = f"http_{response.status_code}"
            return result
        fields = extract_bse_fields(response.text)
        result["returned_identity"] = fields
        result["classification_present"] = bool(fields.get("industry"))
        returned_code = fields.get("security_code")
        result["scrip_code_match"] = (
            None if not returned_code else returned_code.strip().lstrip("0") == scrip_code.lstrip("0")
        )
        # Do not claim an identity match if the endpoint did not echo a usable code/ISIN.
        if returned_code and not result["scrip_code_match"]:
            result["error"] = "identity_mismatch_scrip_code"
        elif not returned_code and not fields.get("isin"):
            result["identity_validation"] = "WEAK_NAME_ONLY_OR_UNAVAILABLE"
        else:
            result["identity_validation"] = "CODE_OR_ISIN_PRESENT"
    except requests.RequestException as exc:
        result["error"] = f"request_error:{type(exc).__name__}:{exc}"
    finally:
        result["elapsed_ms"] = round((time.monotonic() - started) * 1000)
    return result


def main():
    print(json.dumps({"smoke_test": "START", "runner": "github_actions", "pid": os.getpid()}))
    results = []
    for case in NSE_CASES:
        results.append(nse_smoke(case))
    for code in BSE_CASES:
        results.append(bse_smoke(code))
    print(json.dumps({"smoke_test": "RESULTS", "results": results}, indent=2, sort_keys=True))
    failures = [r for r in results if r.get("error")]
    print(json.dumps({"smoke_test": "END", "cases": len(results), "errors": len(failures)}))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
