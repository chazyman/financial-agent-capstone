# /// script
# requires-python = ">=3.12"
# dependencies = [
#   "httpx==0.28.1",
#   "beautifulsoup4==4.12.3",
#   "pypdf==5.1.0",
# ]
# ///
"""Check 8: the Stooq, Stockanalysis and SPDR downloads work within their terms.

Assumption (DESIGN section 2 and the `config/sources.yaml` templates of section 10):
prices come from Stooq CSVs, equity fundamentals from a Stockanalysis download, and ETF
fund documents (fact sheet, prospectus, annual report) from the SPDR site, each by
a plain automated download that the site allows.

Run from spikes/phase0 (network only; no `.env`, no key, nothing saved outside the container):

    docker run --rm \
      --user "$(id -u):$(id -g)" -e HOME=/tmp -e UV_CACHE_DIR=/cache/uv \
      -v "$HOME/.cache/uv":/cache/uv -v "$PWD":/spike:ro \
      ghcr.io/astral-sh/uv:python3.12-bookworm-slim \
      uv run /spike/08_source_downloads.py [spdr2]

Without an argument it runs the first pass (the three sources). `spdr2` runs the second
pass, for SPDR only.

PASS CRITERION, FIRST PASS (approved by the user before it was run; no Stooq key)

Politeness: the User-Agent says what the script is, one request a second at most, each
URL fetched once, and a hard cap of MAX_REQUESTS requests in all (about 60 are expected:
the 11 SPDR funds alone take a fund page and up to three documents each).

Stooq
  - `robots.txt` permits the path.
  - `/q/d/l/?s=aapl.us&i=d` and `...s=xlk.us...` each return CSV with the header
    `Date,Open,High,Low,Close,Volume`, at least 240 rows, and a last date within 7 days of
    the run.
  - If an HTML page comes back instead, the script records what it says, and the
    template fails as written.

Stockanalysis
  - `robots.txt` permits the page; the AAPL financials page offers a CSV or Excel download
    that works without signing in and returns a parseable file. That also settles the URL
    pattern of `config/sources.yaml`.
  - FAIL if there are only HTML tables, or if a sign-in or a paid plan is needed (the
    fallback is SEC `companyfacts`, which already supplies equity fundamentals).

SPDR
  - For all 11 funds, the fact sheet, the prospectus and the annual report each return a
    real PDF (`%PDF` header, over 10 KB). Each URL is found from the fund page's static
    HTML, without JavaScript or sign-in, and `robots.txt` permits it.
  - The sha256 of the prospectus and of the annual report is recorded for all 11 funds,
    which settles whether the funds share them (review topic 7).
  - For XLK only, the script also probes the document API address seen in a search
    (`/api/documents/by-fullname/...`) and records the outcome. That is an observation and
    earns no credit.

Terms
  - A script cannot judge terms. For each site it fetches the terms page that the home page
    links to, and prints every sentence that mentions automated access, robots, crawling,
    scraping, downloading, redistribution or commercial use. The user decides: "within
    terms" means nothing prohibits an automated download of these files for personal,
    non-commercial coursework, and nothing downloaded is committed or redistributed.

RESULT: PASS is printed only if Stooq, Stockanalysis and SPDR all pass their technical
criterion; the terms verdict stays with the user. Each source has its own verdict, and a
failed source names its fallback in the README ("alternative sources").

PASS CRITERION, SECOND PASS, SPDR ONLY (`spdr2`; approved by the user after the first pass)

The first pass found that sectorspdrs.com redirects to www.ssga.com, that fact sheets are
plain PDF links, and that the prospectus and annual-report links are hash links into a
JavaScript viewer. Politeness as before, with a cap of 50 requests (about 30 are expected).
  1. Robots: no URL forbidden by the robots.txt of ssga.com or sectorspdrs.com is requested.
  2. Fact sheets: `https://www.ssga.com/library-content/products/factsheets/etfs/us/
     factsheet-us-en-{ticker}.pdf` returns a real PDF (`%PDF`, over 10 KB) for all 11 funds.
  3. Trust-wide documents: the script tries a fixed list of names on
     `https://www.sectorspdrs.com/api/documents/by-name/{name}`: Prospectus, Annual Report,
     Shareholder Report, Annual Financial Statements and Other Information. It records each
     answer's status, content type and `%PDF`. This part passes if "Prospectus" returns a
     real PDF and at least one of the other three does. (A control, "Statement of
     Additional Information", the name seen in a search, is tried too, to tell a wrong
     name from a dead API; it earns no credit.)
  4. Shared documents, recorded: `pypdf` reads the first 40 pages of each PDF found and
     counts which of the 11 funds' names appear ("<sector> Select Sector SPDR", whether
     the word after is Fund or ETF). "Shared" means all 11 appear in those pages. The
     sha256 is recorded too.
  5. Terms, recorded: the keyword sentences of the terms page that the ssga.com home page or
     its sitemap links to. If none is found, the page is read by hand and quoted.
SPDR second pass passes if 1, 2 and 3 hold; 4 and 5 are recorded for the user to judge.
If part 3 fails, the next route is a separate check on the trust's SEC filings (CIK 1064641).

RECORD OF RUNS
  - First pass (2026-10-12): Stooq FAIL, Stockanalysis FAIL, SPDR FAIL (partial: 11 fact
    sheets passed; prospectus and annual report not found). 36 of 80 requests. Faults of the
    script: it fetched the two Stooq URLs before acting on the robots answer, and one terms
    link led to a news article. The script then refused every URL that robots forbids.
  - Second pass, `spdr2` (2026-10-12): FAIL. 22 of 50 requests. Fact sheets: 11 of 11 pass. The
    by-name API answered HTML or 404 and redirected to ssga.com/fr/en_gb, so no prospectus or
    annual report was found. Fault of the script: its terms discovery picked two articles; the
    terms page was read by hand. The SEC route is check 15.
"""

import hashlib
import re
import sys
import time
from datetime import date
from importlib.metadata import version
from urllib import robotparser
from urllib.parse import unquote, urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

UA = "finrag-capstone-check8/1.0 (coursework; non-commercial; one request a second)"
MAX_REQUESTS = 80
FUNDS = ["XLC", "XLY", "XLP", "XLE", "XLF", "XLV", "XLI", "XLB", "XLRE", "XLK", "XLU"]
KINDS = ["fact sheet", "prospectus", "annual report"]

_client = httpx.Client(headers={"User-Agent": UA}, follow_redirects=True, timeout=30)
_last = 0.0
_count = 0
_robots: dict[str, robotparser.RobotFileParser | None] = {}
_cache: dict[str, httpx.Response | None] = {}


class BudgetExceeded(Exception):
    pass


def get(url: str) -> httpx.Response | None:
    """One polite GET: cached by URL, one a second, counted against MAX_REQUESTS."""
    global _last, _count
    if url in _cache:
        return _cache[url]
    if not url.endswith("/robots.txt") and not allowed(url):
        print(f"    skipped, robots forbids: {url}")
        _cache[url] = None
        return None
    if _count >= MAX_REQUESTS:
        raise BudgetExceeded(f"{MAX_REQUESTS} requests used")
    wait = 1.0 - (time.monotonic() - _last)
    if wait > 0:
        time.sleep(wait)
    _count += 1
    try:
        r = _client.get(url)
    except httpx.HTTPError as e:
        print(f"    request failed: {url} -> {type(e).__name__}: {e}")
        r = None
    _last = time.monotonic()
    _cache[url] = r
    return r


def allowed(url: str) -> bool:
    """robots.txt of the URL's host, fetched once. A missing file (4xx) allows; a 5xx forbids."""
    p = urlparse(url)
    base = f"{p.scheme}://{p.netloc}"
    if base not in _robots:
        r = get(base + "/robots.txt")
        rp = robotparser.RobotFileParser()
        if r is None or r.status_code >= 500:
            rp.parse(["User-agent: *", "Disallow: /"])
            print(f"    robots.txt of {p.netloc}: unreachable or 5xx -> treated as forbidden")
        elif r.status_code >= 400:
            rp.parse([])
            print(f"    robots.txt of {p.netloc}: HTTP {r.status_code} -> no rules")
        else:
            rp.parse(r.text.splitlines())
            lines = [l.strip() for l in r.text.splitlines() if l.strip().lower().startswith(("disallow", "user-agent", "crawl-delay"))]
            print(f"    robots.txt of {p.netloc}: {len(lines)} rule lines; first 8: {lines[:8]}")
        _robots[base] = rp
    return _robots[base].can_fetch(UA, url)


# ---------------------------------------------------------------- terms

TERMS_LINK = re.compile(r"terms|legal|conditions|disclaimer|regulamin|user agreement", re.I)
SKIP_LINK = re.compile(r"privacy|cookie|prywatno", re.I)
KEYWORDS = re.compile(
    r"automat|robot|crawl|scrap|spider|\bbot\b|download|redistribut|commercial|personal use|"
    r"non-commercial|data mining|harvest|pobiera|kopiow|rozpowszech|komercyj", re.I)


def terms_excerpts(name: str, home: str | list[str], guesses: list[str]) -> None:
    print(f"  terms of {name}:")
    urls: list[str] = []
    for page in [home] if isinstance(home, str) else home:
        if urls:
            break
        r = get(page)
        if r is not None and r.status_code == 200:
            soup = BeautifulSoup(r.text, "html.parser")
            for a in soup.find_all("a", href=True):
                label = a.get_text(" ", strip=True)
                if (TERMS_LINK.search(label) or TERMS_LINK.search(a["href"])) and not SKIP_LINK.search(label + a["href"]):
                    u = urljoin(str(r.url), a["href"])
                    if u not in urls and u.startswith("http"):
                        urls.append(u)
    urls = (urls or guesses)[:2]
    if not urls:
        print("    no terms link found on the home page")
    for u in urls:
        t = get(u)
        if t is None or t.status_code != 200:
            print(f"    {u}: HTTP {t.status_code if t is not None else 'error'}")
            continue
        text = BeautifulSoup(t.text, "html.parser").get_text(" ", strip=True)
        sentences = [s.strip() for s in re.split(r"(?<=[.;])\s+", text) if KEYWORDS.search(s)]
        print(f"    {u}: {len(text)} characters of text, {len(sentences)} sentences mention the keywords")
        for s in sentences[:15]:
            print(f"      > {s[:300]}")
        if not text.strip():
            print("      (no text in the static HTML: the page needs JavaScript)")


# ---------------------------------------------------------------- Stooq

def check_stooq() -> bool:
    print("Stooq")
    ok = True
    for sym in ["aapl.us", "xlk.us"]:
        url = f"https://stooq.com/q/d/l/?s={sym}&i=d"
        perm = allowed(url)
        if not perm:
            print(f"  {sym}: robots FORBIDS the path; not requested")
            ok = False
            continue
        r = get(url)
        if r is None:
            print(f"  {sym}: no response")
            ok = False
            continue
        lines = r.text.strip().splitlines()
        is_csv = bool(lines) and lines[0].strip() == "Date,Open,High,Low,Close,Volume"
        rows = len(lines) - 1 if is_csv else 0
        age = None
        if is_csv and rows:
            try:
                age = (date.today() - date.fromisoformat(lines[-1].split(",")[0])).days
            except ValueError:
                pass
        good = perm and r.status_code == 200 and is_csv and rows >= 240 and age is not None and age <= 7
        print(f"  {sym}: robots {'allows' if perm else 'FORBIDS'}; HTTP {r.status_code}; content-type {r.headers.get('content-type')}; "
              f"CSV header {is_csv}; rows {rows}; last row {age} days old -> {'ok' if good else 'NOT ok'}")
        if not is_csv:
            notice = re.sub(r"\s+", " ", BeautifulSoup(r.text, "html.parser").get_text(" ", strip=True))[:300]
            print(f"    what came back instead: {notice!r}")
        ok = ok and good
    terms_excerpts("Stooq", "https://stooq.com/", [])
    print(f"  Stooq {'PASS' if ok else 'FAIL'}")
    return ok


# ---------------------------------------------------------------- Stockanalysis

def looks_parseable(r: httpx.Response) -> str | None:
    body = r.content
    if body[:2] == b"PK":
        return "xlsx"
    head = body[:2000].decode("utf-8", "replace").lstrip()
    lines = head.splitlines()
    if len(lines) >= 2 and not head.startswith("<") and lines[0].count(",") >= 1:
        return "csv"
    return None


def check_stockanalysis() -> bool:
    print("Stockanalysis")
    page = "https://stockanalysis.com/stocks/aapl/financials/"
    perm = allowed(page)
    r = get(page)
    ok = False
    if r is None or r.status_code != 200:
        print(f"  {page}: HTTP {r.status_code if r is not None else 'error'}")
    else:
        soup = BeautifulSoup(r.text, "html.parser")
        cands = []
        for el in soup.find_all(["a", "button"]):
            label = el.get_text(" ", strip=True)
            href = el.get("href", "")
            if re.search(r"download|export|excel|csv|xlsx", f"{label} {href}", re.I):
                cands.append((el.name, label[:40], href[:140]))
        print(f"  page: robots {'allows' if perm else 'FORBIDS'}; HTTP 200; {len(cands)} download-like elements")
        for c in cands[:15]:
            print(f"    {c}")
        tried = 0
        for _, _, href in cands:
            if not href or tried >= 3 or not re.search(r"\.csv|\.xlsx|download|export|format=", href, re.I):
                continue
            u = urljoin(page, href)
            tried += 1
            if not allowed(u):
                print(f"    {u}: robots FORBIDS")
                continue
            d = get(u)
            kind = looks_parseable(d) if d is not None and d.status_code == 200 else None
            print(f"    {u}: HTTP {d.status_code if d is not None else 'error'}; parseable file: {kind}")
            if kind and perm:
                ok = True
                break
        if not ok and tried == 0:
            print("    no downloadable file address in the static HTML (an export button needs JavaScript or a paid plan)")
    terms_excerpts("Stockanalysis", "https://stockanalysis.com/", ["https://stockanalysis.com/terms-of-use/"])
    print(f"  Stockanalysis {'PASS' if ok else 'FAIL'}")
    return ok


# ---------------------------------------------------------------- SPDR

DOC_REF = re.compile(r"""["'(]([^"'()\s<>]*(?:\.pdf|/api/documents/)[^"'()\s<>]*)""", re.I)


def fund_documents(page_url: str, html: str) -> dict[str, str]:
    """Static-HTML document references on a fund page, classified by their address or link text."""
    refs: dict[str, str] = {}
    soup = BeautifulSoup(html, "html.parser")
    labelled = []
    for a in soup.find_all("a", href=True):
        labelled.append((a["href"], a.get_text(" ", strip=True)))
    labelled += [(m, "") for m in DOC_REF.findall(html)]
    for href, label in labelled:
        text = unquote(f"{href} {label}").lower()
        if ".pdf" not in text and "/api/documents/" not in text:
            continue
        kind = None
        if "fact" in text and "sheet" in text:
            kind = "fact sheet"
        elif "prospectus" in text:
            kind = "prospectus"
        elif "annual" in text or "shareholder report" in text:
            kind = "annual report"
        if kind and kind not in refs:
            refs[kind] = urljoin(page_url, href.replace("\\u002F", "/"))
    return refs


def real_pdf(url: str) -> tuple[bool, str, int]:
    if not allowed(url):
        return False, "robots forbids", 0
    r = get(url)
    if r is None or r.status_code != 200:
        return False, f"HTTP {r.status_code if r is not None else 'error'}", 0
    body = r.content
    good = body[:4] == b"%PDF" and len(body) > 10_000
    return good, hashlib.sha256(body).hexdigest()[:16], len(body)


def check_spdr() -> bool:
    print("SPDR")
    base = "https://www.sectorspdrs.com"
    page_pattern = None
    for pat in ["/mainfund/{t}", "/fund/{t}", "/funds/{t}"]:
        u = base + pat.format(t="XLK")
        if allowed(u):
            r = get(u)
            if r is not None and r.status_code == 200:
                page_pattern = pat
                break
        else:
            print(f"  {u}: robots FORBIDS")
    if page_pattern is None:
        print("  no fund page address answered for XLK")
        terms_excerpts("SPDR", base + "/", [])
        print("  SPDR FAIL")
        return False

    results: dict[str, dict[str, tuple[bool, str, int, str]]] = {}
    for t in FUNDS:
        u = base + page_pattern.format(t=t)
        r = get(u)
        if r is None or r.status_code != 200:
            print(f"  {t}: fund page HTTP {r.status_code if r is not None else 'error'}")
            results[t] = {}
            continue
        refs = fund_documents(u, r.text)
        js_only = not refs and "__NEXT_DATA__" in r.text
        print(f"  {t}: documents found in the static HTML: {sorted(refs)}" + ("  (page is rendered by JavaScript)" if js_only else ""))
        if t == "XLK" and not refs:
            print("  XLK shows nothing in static HTML: the other funds will not be tried")
            results = {"XLK": {}}
            break
        results[t] = {}
        for k in KINDS:
            if k in refs:
                good, h, n = real_pdf(refs[k])
                results[t][k] = (good, h, n, refs[k])
            else:
                results[t][k] = (False, "not found on the page", 0, "")

    # observation only: the API address seen in a search, for XLK
    for k, name in [("fact sheet", "Fact%20Sheet"), ("prospectus", "Prospectus"), ("annual report", "Annual%20Report")]:
        api = f"{base}/api/documents/by-fullname/Sector%20Documents/XLK%20-%20Technology%20Documents/{name}"
        if allowed(api):
            r = get(api)
            print(f"  observation, XLK API {k}: HTTP {r.status_code if r is not None else 'error'}; "
                  f"content-type {r.headers.get('content-type') if r is not None else None}; "
                  f"%PDF {r.content[:4] == b'%PDF' if r is not None else None}")

    ok = len(results) == len(FUNDS)
    print("  fund      " + "  ".join(f"{k:<14}" for k in KINDS))
    for t, row in results.items():
        cells = []
        for k in KINDS:
            good, h, n, _ = row.get(k, (False, "-", 0, ""))
            ok = ok and good
            cells.append(f"{'ok ' if good else 'NO '}{h[:11]:<11}")
        print(f"  {t:<9} " + "  ".join(f"{c:<14}" for c in cells))
    for k in ["prospectus", "annual report"]:
        urls = {row[k][3] for row in results.values() if k in row and row[k][3]}
        hashes = {row[k][1] for row in results.values() if k in row and row[k][0]}
        print(f"  {k}: {len(urls)} distinct address(es), {len(hashes)} distinct content hash(es) over {len(results)} fund(s)")
    terms_excerpts("SPDR", base + "/", [])
    print(f"  SPDR {'PASS' if ok else 'FAIL'}")
    return ok


SECTORS = {
    "XLC": "Communication Services", "XLY": "Consumer Discretionary", "XLP": "Consumer Staples",
    "XLE": "Energy", "XLF": "Financial", "XLV": "Health Care", "XLI": "Industrial",
    "XLB": "Materials", "XLRE": "Real Estate", "XLK": "Technology", "XLU": "Utilities",
}
API = "https://www.sectorspdrs.com/api/documents/by-name/"
PROSPECTUS_NAMES = ["Prospectus"]
ANNUAL_NAMES = ["Annual Report", "Shareholder Report", "Annual Financial Statements and Other Information"]
CONTROL_NAMES = ["Statement of Additional Information"]


def fetch_pdf(url: str) -> tuple[bool, str, httpx.Response | None]:
    r = get(url)
    if r is None:
        return False, "no response (error, or robots forbids)", None
    good = r.status_code == 200 and r.content[:4] == b"%PDF" and len(r.content) > 10_000
    return good, f"HTTP {r.status_code}; {r.headers.get('content-type')}; {len(r.content)} bytes; %PDF {r.content[:4] == b'%PDF'}", r


def funds_named(body: bytes) -> tuple[int, list[str]]:
    import io
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(body))
    text = " ".join((p.extract_text() or "") for p in reader.pages[:40])
    text = re.sub(r"\s+", " ", text)
    found = [t for t, sector in SECTORS.items() if re.search(rf"{sector}\s+Select\s+Sector\s+SPDR", text, re.I)]
    return len(reader.pages), found


def check_spdr2() -> bool:
    from urllib.parse import quote

    print("SPDR, second pass")
    ok = True

    print("  fact sheets (template on ssga.com):")
    for t in FUNDS:
        url = f"https://www.ssga.com/library-content/products/factsheets/etfs/us/factsheet-us-en-{t.lower()}.pdf"
        good, note_, _ = fetch_pdf(url)
        print(f"    {t}: {'ok ' if good else 'NO '} {note_}")
        ok = ok and good

    print("  trust-wide documents (sectorspdrs.com API, by name):")
    found: dict[str, tuple[str, httpx.Response]] = {}
    verdict = {"prospectus": False, "annual report": False}
    for group, names in [("prospectus", PROSPECTUS_NAMES), ("annual report", ANNUAL_NAMES), ("control", CONTROL_NAMES)]:
        for name in names:
            url = API + quote(name)
            good, note_, r = fetch_pdf(url)
            print(f"    [{group}] {name}: {'ok ' if good else 'NO '} {note_}" + (f"; final address {r.url}" if r is not None and str(r.url) != url else ""))
            if good:
                found[name] = (group, r)
                if group in verdict:
                    verdict[group] = True
    ok = ok and all(verdict.values())

    print("  shared documents (recorded):")
    for name, (group, r) in found.items():
        try:
            pages, funds = funds_named(r.content)
            print(f"    {name}: sha256 {hashlib.sha256(r.content).hexdigest()[:16]}; {pages} pages; "
                  f"{len(funds)} of 11 funds named in the first 40 pages: {funds}")
        except Exception as e:  # noqa: BLE001
            print(f"    {name}: could not read the PDF text ({type(e).__name__}: {e})")

    terms_excerpts("SPDR (ssga.com)", ["https://www.ssga.com/us/en/intermediary/", "https://www.ssga.com/us/en/intermediary/sitemap"], [])
    print(f"  prospectus ok: {verdict['prospectus']}; annual report ok: {verdict['annual report']}")
    print(f"  SPDR second pass {'PASS' if ok else 'FAIL'}")
    return ok


def main() -> None:
    global MAX_REQUESTS
    which = sys.argv[1] if len(sys.argv) > 1 else "pass1"
    print("versions:", {p: version(p) for p in ["httpx", "beautifulsoup4", "pypdf"]}, "today:", date.today(), "pass:", which)
    results = {}
    if which == "spdr2":
        MAX_REQUESTS = 50
        steps = [("SPDR second pass", check_spdr2)]
    elif which == "pass1":
        steps = [("Stooq", check_stooq), ("Stockanalysis", check_stockanalysis), ("SPDR", check_spdr)]
    else:
        sys.exit(f"unknown pass {which!r}")
    for name, fn in steps:
        try:
            results[name] = fn()
        except BudgetExceeded as e:
            print(f"  {name}: stopped, {e}")
            results[name] = False
        except Exception as e:  # noqa: BLE001
            import traceback
            traceback.print_exc()
            results[name] = False
    print(f"requests used: {_count} of {MAX_REQUESTS}")
    print("summary:", {k: ("PASS" if v else "FAIL") for k, v in results.items()}, "(terms: the user's verdict)")
    print("RESULT: PASS" if all(results.values()) else "RESULT: FAIL")


if __name__ == "__main__":
    main()
