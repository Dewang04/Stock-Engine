import json
import os
import sys
import time

import requests

NSE_CASES = [
    {"symbol": "CONCORDBIO", "isin": "INE338H01029"},
    {"symbol": "AARTIPHARM", "isin": "INE0LRU01027"},
]
BSE_CASES = [
    {"scrip_code": "539997", "isin": "INE552U01010"},
    {"scrip_code": "524500", "isin": "INE729D01010"},
    {"scrip_code": "532989", "isin": None},
]

NSE_HOME = "https://www.nseindia.com/"
NSE_API = "https://www.nseindia.com/api/quote-equity"
BSE_API = "https://api.bseindia.com/BseIndiaAPI/api/ListofScripData/w"

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
        result["returned_identity"] = {"symbol": info.get("symbol"), "companyName": info.get("companyName"), "isin": returned_isin}
        result["industryInfo"] = industry
        result["isin_match"] = None if not expected_isin or not returned_isin else returned_isin.upper() == expected_isin.upper()
        result["classification_present"] = any(bool(industry.get(k)) for k in ("macro", "sector", "industry", "basicIndustry"))
        result["raw_response_keys"] = sorted(data.keys())
        if expected_isin and returned_isin and returned_isin.upper() != expected_isin.upper():
            result["error"] = "identity_mismatch_isin"
    except requests.RequestException as exc:
        result["error"] = f"request_error:{type(exc).__name__}:{exc}"
    finally:
        result["elapsed_ms"] = round((time.monotonic() - started) * 1000)
        session.close()
    return result


def bse_smoke(case):
    code = case["scrip_code"]
    expected_isin = case.get("isin")
    started = time.monotonic()
    result = {"exchange": "XBOM", "scrip_code": code}
    headers = {
        "Host": "api.bseindia.com",
        "Referer": "https://www.bseindia.com/corporates/ann.html",
        "User-Agent": UA,
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "Origin": "https://www.bseindia.com",
    }
    try:
        response = requests.get(
            BSE_API,
            params={"scripcode": code, "Group": "", "industry": "", "segment": "Equity", "status": "Active"},
            headers=headers,
            timeout=20,
        )
        result["http_status"] = response.status_code
        result["content_type"] = response.headers.get("content-type")
        data = safe_json(response)
        if not isinstance(data, (list, dict)):
            result["error"] = "api_non_json"
            result["response_preview"] = response.text[:500]
            return result
        rows = data if isinstance(data, list) else data.get("Table") or data.get("table") or []
        result["row_count"] = len(rows) if isinstance(rows, list) else None
        if not isinstance(rows, list) or not rows:
            result["error"] = "no_rows"
            return result
        row = rows[0]
        result["returned_keys"] = sorted(row.keys()) if isinstance(row, dict) else []
        if not isinstance(row, dict):
            result["error"] = "row_not_object"
            return result
        # Capture likely identity/classification fields without trusting positional data.
        normalized = {str(k).strip().lower().replace(" ", "_"): v for k, v in row.items()}
        result["returned_identity"] = {
            "security_code": normalized.get("scrip_code") or normalized.get("security_code") or normalized.get("scripcode"),
            "isin": normalized.get("isin") or normalized.get("isin_number"),
            "scrip_name": normalized.get("scrip_name") or normalized.get("security_name") or normalized.get("company_name") or normalized.get("scrip_name_"),
            "industry": normalized.get("industry") or normalized.get("industry_name"),
        }
        returned_code = str(result["returned_identity"]["security_code"] or "").strip()
        returned_isin = result["returned_identity"]["isin"]
        result["scrip_code_match"] = returned_code.lstrip("0") == code.lstrip("0") if returned_code else None
        result["isin_match"] = None if not expected_isin or not returned_isin else str(returned_isin).upper() == expected_isin.upper()
        result["classification_present"] = bool(result["returned_identity"]["industry"])
        if returned_code and not result["scrip_code_match"]:
            result["error"] = "identity_mismatch_scrip_code"
        elif expected_isin and returned_isin and not result["isin_match"]:
            result["error"] = "identity_mismatch_isin"
        elif not returned_code and not returned_isin:
            result["error"] = "identity_unavailable"
    except requests.RequestException as exc:
        result["error"] = f"request_error:{type(exc).__name__}:{exc}"
    finally:
        result["elapsed_ms"] = round((time.monotonic() - started) * 1000)
    return result


def main():
    print(json.dumps({"smoke_test": "START", "runner": "github_actions", "pid": os.getpid()}))
    results = [nse_smoke(c) for c in NSE_CASES] + [bse_smoke(c) for c in BSE_CASES]
    print(json.dumps({"smoke_test": "RESULTS", "results": results}, indent=2, sort_keys=True))
    failures = [r for r in results if r.get("error")]
    print(json.dumps({"smoke_test": "END", "cases": len(results), "errors": len(failures)}))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
