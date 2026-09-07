import csv
import io
import re
import requests

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/128 Safari/537.36"
TARGETS = {
    "CONCORDBIO": "CONCORDBIO",
    "ALIVUS": "ALIVUS",
}
BSE_CODES = {"539997", "524500", "532989"}
URLS = [
    "https://www.niftyindices.com/IndexConstituent/ind_nifty500list.csv",
    "https://www.niftyindices.com/IndexConstituent/ind_nifty50list.csv",
    "https://www.niftyindices.com/IndexConstituent/ind_niftymidcap150list.csv",
    "https://www.niftyindices.com/IndexConstituent/ind_niftysmallcap250list.csv",
    "https://archives.nseindia.com/content/indices/ind_nifty500list.csv",
]


def norm(v):
    return re.sub(r"[^A-Z0-9]+", "", str(v or "").upper())


def main():
    session = requests.Session()
    session.headers.update({
        "User-Agent": UA,
        "Accept": "text/csv,text/plain,*/*",
        "Referer": "https://www.niftyindices.com/indices/equity/broad-based-indices",
    })
    for url in URLS:
        print(f"\nURL {url}")
        try:
            r = session.get(url, timeout=30)
            print(f"HTTP {r.status_code} bytes={len(r.content)} content_type={r.headers.get('content-type')}")
            if not r.ok:
                continue
            text = r.text.lstrip("\ufeff")
            reader = csv.DictReader(io.StringIO(text))
            rows = list(reader)
            print(f"rows={len(rows)} columns={reader.fieldnames}")
            hits = []
            for row in rows:
                symbol = norm(row.get("Symbol"))
                isin = norm(row.get("ISIN Code"))
                company = norm(row.get("Company Name"))
                if symbol in {norm(x) for x in TARGETS.values()} or any(norm(x) in {symbol, isin, company} for x in TARGETS.values()):
                    hits.append(row)
            if hits:
                print("TARGET_HITS")
                for row in hits:
                    print(row)
        except Exception as exc:
            print(f"ERROR {type(exc).__name__}: {exc}")
    print("\nBSE target codes noted for separate source investigation:", sorted(BSE_CODES))


if __name__ == "__main__":
    main()
