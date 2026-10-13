# /// script
# requires-python = ">=3.12"
# dependencies = [
#   "httpx==0.28.1",
# ]
# ///
"""Check 16: Tiingo and Twelve Data, on their free plans, deliver daily prices for the 19 instruments.

Assumption (DESIGN section 1 and section 10): Tiingo (primary) and Twelve Data (fallback) each return
daily open, high, low, close and volume for the 11 sector ETFs and the 8 equities of the starting
corpus, within the limits of a free key. The run also measures what each API can do, to write the two
capability studies kept in Resources/ (tiingo_capabilities.md, twelvedata_capabilities.md).

Run from spikes/phase0. Each key is read by the command and passed to this one run; nothing is printed
or saved with a key in it. `$OUT` is a scratch folder that receives the shapes the run recorded (no
prices): run_summary.json and twelvedata_openapi_catalogue.json.

    OUT=<a scratch folder>; mkdir -p "$OUT"
    docker run --rm \\
      --user "$(id -u):$(id -g)" -e HOME=/tmp -e UV_CACHE_DIR=/cache/uv \\
      -e TIINGO_API_KEY="$(grep '^TIINGO_API_KEY=' ../../.env | cut -d= -f2-)" \\
      -e TWELVEDATA_API_KEY="$(grep '^TWELVEDATA_API_KEY=' ../../.env | cut -d= -f2-)" \\
      -v "$HOME/.cache/uv":/cache/uv -v "$PWD":/spike:ro -v "$OUT":/out \\
      ghcr.io/astral-sh/uv:python3.12-bookworm-slim \\
      uv run /spike/16_price_providers.py

PASS CRITERION (approved by the user before this script was run; free plans only)

Setup: the standard disposable container; keys passed with -e; each key travels in an HTTP header,
never in a URL, and is scrubbed from anything printed; no response is saved outside the container
except shapes (field names, types, counts), never values.

Per provider, a technical pass:
  1. Auth: a free account call succeeds and the plan and its limits are recorded (Tiingo `/api/test/`;
     Twelve Data `/api_usage`).
  2. All 19 instruments (11 ETFs and 8 equities): daily open, high, low, close and volume, at least 240
     rows inside the last 365 days, and the last date within 5 days of the run.
  3. Sanity: high is at least the open and the close, low is at most both, no duplicate dates, volume
     is not negative.
  4. Quota: no refusal (HTTP 429), and the cost of the run is recorded against the plan.

Recorded, not scored:
  - whether one call can return several tickers;
  - the fields for raw versus adjusted prices;
  - how far the two providers' closes agree, per ticker (raw against raw; adjusted against adjusted
    for two tickers);
  - the first date each provider has for the instruments (supported-tickers list for Tiingo, earliest
    timestamp for Twelve Data) and any ticker not found;
  - which extra endpoints the free plan rejects, from a few probes.

Budgets: Tiingo's free plan allows 50 requests an hour; the script stops at 45 and reads meta for 6
tickers only. Twelve Data allows 8 credits a minute; the script paces at 7 and stops at 120 credits.

Terms are read by hand after the run (storage in a local database for a non-public demo; display and
sharing); the verdict is the user's.

Result: a verdict per provider; `RESULT: PASS` only if both pass items 1 to 4. Which provider is
primary is the user's decision and not a result of this check.

RECORD OF RUNS
  - 2026-10-13: Tiingo PASS (28 requests of 50 an hour), Twelve Data PASS (about 31 credits of 800
    a day), RESULT: PASS. The terms were read by hand afterwards; see the README row of check 16.
"""

import csv
import io
import json
import os
import statistics
import sys
import time
import zipfile
from collections import deque
from datetime import date, timedelta
from importlib.metadata import version

import httpx

ETFS = ["XLC", "XLY", "XLP", "XLE", "XLF", "XLV", "XLI", "XLB", "XLRE", "XLK", "XLU"]
EQUITIES = ["AAPL", "MSFT", "NVDA", "GOOGL", "META", "AMZN", "TSLA", "IONQ"]
ALL = ETFS + EQUITIES
TODAY = date.today()
START = TODAY - timedelta(days=365)
KEYS = {"tiingo": os.environ.get("TIINGO_API_KEY", ""), "twelvedata": os.environ.get("TWELVEDATA_API_KEY", "")}
OUT = "/out" if os.path.isdir("/out") else None
client = httpx.Client(timeout=60, follow_redirects=True)
summary: dict = {"run_date": TODAY.isoformat(), "window_start": START.isoformat()}


def scrub(text: str) -> str:
    for k in KEYS.values():
        if k:
            text = text.replace(k, "***")
    return text


def say(*parts) -> None:
    print(scrub(" ".join(str(p) for p in parts)))


def shape(x, depth: int = 0):
    """The structure of a JSON value: field names and types, list lengths; never the values."""
    if isinstance(x, dict):
        return {k: shape(v, depth + 1) for k, v in list(x.items())[:60]}
    if isinstance(x, list):
        return {"list_of": shape(x[0], depth + 1) if x else None, "length": len(x)}
    return type(x).__name__


def rate_headers(r: httpx.Response) -> list[str]:
    return sorted(h for h in r.headers if any(w in h.lower() for w in ("rate", "limit", "remaining", "credit", "quota")))


class Stop(Exception):
    pass


def evaluate(series: dict[str, list[dict]]) -> tuple[bool, dict]:
    """Items 2 and 3 of the criterion over {ticker: [{date, open, high, low, close, volume}]}."""
    ok = True
    per = {}
    for t in ALL:
        rows = series.get(t, [])
        dates = [r["date"] for r in rows]
        bad = 0
        for r in rows:
            try:
                o, h, l, c, v = (float(r[k]) for k in ("open", "high", "low", "close", "volume"))
                if not (h >= max(o, c) - 1e-9 and l <= min(o, c) + 1e-9 and v >= 0):
                    bad += 1
            except (KeyError, TypeError, ValueError):
                bad += 1
        last_age = (TODAY - date.fromisoformat(max(dates))).days if dates else None
        good = len(rows) >= 240 and last_age is not None and last_age <= 5 and bad == 0 and len(set(dates)) == len(dates)
        per[t] = {"rows": len(rows), "first": min(dates) if dates else None, "last": max(dates) if dates else None,
                  "last_age_days": last_age, "sanity_failures": bad, "duplicate_dates": len(dates) - len(set(dates)), "ok": good}
        ok = ok and good
    return ok, per


# ---------------------------------------------------------------- Tiingo

def run_tiingo() -> tuple[bool, dict[str, list[dict]]]:
    say("Tiingo")
    base = "https://api.tiingo.com"
    headers = {"Authorization": f"Token {KEYS['tiingo']}", "Content-Type": "application/json"}
    calls = 0
    rec: dict = {"endpoints": {}, "rate_headers_seen": set()}

    def call(path: str, params: dict | None = None) -> httpx.Response:
        nonlocal calls
        if calls >= 45:
            raise Stop("45 requests used")
        calls += 1
        r = client.get(base + path, headers=headers, params=params)
        rec["rate_headers_seen"].update(rate_headers(r))
        time.sleep(0.5)
        return r

    ok = True
    r = call("/api/test/")
    say(f"  1. auth: HTTP {r.status_code}; body {scrub(r.text[:100])!r}; rate headers {rate_headers(r)}")
    rec["endpoints"]["/api/test/"] = {"status": r.status_code}
    ok = ok and r.status_code == 200

    # supported tickers: a file on another host, no key, no request counted
    z = client.get("https://apimedia.tiingo.com/docs/tiingo/daily/supported_tickers.zip")
    cover = {}
    if z.status_code == 200:
        with zipfile.ZipFile(io.BytesIO(z.content)) as zf:
            with zf.open(zf.namelist()[0]) as f:
                reader = csv.DictReader(io.TextIOWrapper(f, "utf-8"))
                cols = reader.fieldnames
                for row in reader:
                    if row["ticker"] in ALL:
                        cover[row["ticker"]] = {k: row.get(k) for k in ("exchange", "assetType", "priceCurrency", "startDate", "endDate")}
        say(f"     supported_tickers.zip columns {cols}; {len(cover)} of 19 found")
        missing = [t for t in ALL if t not in cover]
        for t in ALL:
            if t in cover:
                say(f"       {t}: {cover[t]}")
        say(f"     not in the list: {missing}")
        rec["supported_tickers"] = {"columns": cols, "found": cover, "missing": missing}
    else:
        say(f"     supported_tickers.zip: HTTP {z.status_code}")

    series: dict[str, list[dict]] = {}
    refused = False
    for t in ALL:
        r = call(f"/tiingo/daily/{t}/prices", {"startDate": START.isoformat(), "endDate": TODAY.isoformat()})
        if r.status_code == 429:
            say(f"  {t}: HTTP 429, stopping: {scrub(r.text[:150])!r}")
            refused = True
            break
        if r.status_code != 200:
            say(f"  {t}: HTTP {r.status_code}: {scrub(r.text[:150])!r}")
            continue
        data = r.json()
        series[t] = [{"date": d["date"][:10], "open": d.get("open"), "high": d.get("high"), "low": d.get("low"),
                      "close": d.get("close"), "volume": d.get("volume"), "adjClose": d.get("adjClose")} for d in data]
        if "/tiingo/daily/{t}/prices" not in rec["endpoints"] and data:
            rec["endpoints"]["/tiingo/daily/{t}/prices"] = {"status": 200, "row_shape": shape(data[0]), "content_type": r.headers.get("content-type")}
    good, per = evaluate(series)
    for t, p in per.items():
        say(f"  {t:<5} rows {p['rows']:>3}  {p['first']}..{p['last']}  age {p['last_age_days']}  bad {p['sanity_failures']}  dup {p['duplicate_dates']}  {'ok' if p['ok'] else 'NOT ok'}")
    ok = ok and good and not refused
    rec["per_ticker"] = per

    for t in ["XLK", "XLRE", "XLC", "AAPL", "GOOGL", "IONQ"]:
        r = call(f"/tiingo/daily/{t}")
        if r.status_code == 200:
            rec["endpoints"].setdefault("/tiingo/daily/{t}", {"status": 200, "shape": shape(r.json())})
            say(f"     meta {t}: fields {sorted(r.json())}; {r.json().get('startDate')}..{r.json().get('endDate')}")
        else:
            say(f"     meta {t}: HTTP {r.status_code}")

    for path, params in [("/iex/", {"tickers": "AAPL,MSFT"}), ("/tiingo/fundamentals/definitions", None)]:
        try:
            r = call(path, params)
        except Stop:
            break
        entry = {"status": r.status_code, "content_type": r.headers.get("content-type")}
        if r.status_code == 200:
            try:
                entry["shape"] = shape(r.json())
            except ValueError:
                pass
        else:
            entry["message"] = scrub(r.text[:200])
        rec["endpoints"][path] = entry
        say(f"     probe {path}: HTTP {r.status_code} {entry.get('message', '')}")

    say(f"  requests used: {calls} of 45 (free plan: 50 an hour); rate headers seen: {sorted(rec['rate_headers_seen'])}")
    rec["requests_used"] = calls
    rec["rate_headers_seen"] = sorted(rec["rate_headers_seen"])
    rec["pass"] = ok
    summary["tiingo"] = rec
    say(f"  Tiingo {'PASS' if ok else 'FAIL'}")
    return ok, series


# ---------------------------------------------------------------- Twelve Data

def run_twelvedata(tiingo: dict[str, list[dict]]) -> bool:
    say("Twelve Data")
    base = "https://api.twelvedata.com"
    headers = {"Authorization": f"apikey {KEYS['twelvedata']}"}
    window: deque[tuple[float, int]] = deque()
    spent = 0
    rec: dict = {"endpoints": {}, "rate_headers_seen": set()}

    def call(path: str, params: dict, cost: int) -> httpx.Response:
        nonlocal spent
        if spent + cost > 120:
            raise Stop("120 credits would be exceeded")
        while True:
            now = time.monotonic()
            while window and now - window[0][0] > 61:
                window.popleft()
            if sum(c for _, c in window) + cost <= 7:
                break
            time.sleep(1)
        window.append((time.monotonic(), cost))
        spent += cost
        r = client.get(base + path, headers=headers, params=params)
        rec["rate_headers_seen"].update(rate_headers(r))
        return r

    ok = True
    r = call("/api_usage", {}, 1)
    usage_before = r.json() if r.status_code == 200 else {}
    say(f"  1. auth: HTTP {r.status_code}; /api_usage fields {sorted(usage_before)}")
    say(f"     usage before: { {k: v for k, v in usage_before.items() if not isinstance(v, (dict, list))} }")
    rec["endpoints"]["/api_usage"] = {"status": r.status_code, "shape": shape(usage_before)}
    ok = ok and r.status_code == 200

    # the catalogue, from the OpenAPI file (no credits)
    spec = client.get(base + "/doc/swagger/openapi.json")
    if spec.status_code == 200:
        paths = spec.json().get("paths", {})
        cat = {}
        for p, ops in paths.items():
            for method, op in ops.items():
                if isinstance(op, dict):
                    cat[p] = {"method": method, "summary": op.get("summary"), "tags": op.get("tags"),
                              "starting_plan": op.get("x-starting-plan"), "x": {k: v for k, v in op.items() if k.startswith("x-")},
                              "params": [q.get("name") for q in op.get("parameters", []) if isinstance(q, dict)]}
        say(f"     OpenAPI: {len(cat)} paths; starting plans seen: {sorted({str(v['starting_plan']) for v in cat.values()})}")
        if OUT:
            with open(os.path.join(OUT, "twelvedata_openapi_catalogue.json"), "w") as f:
                json.dump(cat, f, indent=1)
    else:
        say(f"     OpenAPI: HTTP {spec.status_code}")

    series: dict[str, list[dict]] = {}
    refused = False
    chunks = [ALL[i:i + 7] for i in range(0, len(ALL), 7)]
    for ch in chunks:
        r = call("/time_series", {"symbol": ",".join(ch), "interval": "1day", "start_date": START.isoformat(),
                                  "end_date": TODAY.isoformat(), "order": "asc", "adjust": "none"}, len(ch))
        if r.status_code == 429:
            say(f"  {ch}: HTTP 429: {scrub(r.text[:150])!r}")
            refused = True
            break
        body = r.json()
        rec["endpoints"].setdefault("/time_series (batch)", {"status": r.status_code, "shape": shape(body)})
        entries = {ch[0]: body} if "values" in body or "code" in body else body
        for t, e in entries.items():
            if isinstance(e, dict) and e.get("status") == "ok" or (isinstance(e, dict) and "values" in e):
                series[t] = [{"date": v["datetime"][:10], "open": v["open"], "high": v["high"], "low": v["low"],
                              "close": v["close"], "volume": v["volume"]} for v in e["values"]]
            else:
                say(f"  {t}: {scrub(json.dumps(e)[:200])}")
    good, per = evaluate(series)
    for t, p in per.items():
        say(f"  {t:<5} rows {p['rows']:>3}  {p['first']}..{p['last']}  age {p['last_age_days']}  bad {p['sanity_failures']}  dup {p['duplicate_dates']}  {'ok' if p['ok'] else 'NOT ok'}")
    ok = ok and good and not refused
    rec["per_ticker"] = per

    # adjusted series for two tickers, against Tiingo's adjClose
    adj_diff = {}
    r = call("/time_series", {"symbol": "XLK,AAPL", "interval": "1day", "start_date": START.isoformat(),
                              "end_date": TODAY.isoformat(), "order": "asc", "adjust": "all"}, 2)
    if r.status_code == 200:
        body = r.json()
        for t in ("XLK", "AAPL"):
            e = body.get(t, {})
            td = {v["datetime"][:10]: float(v["close"]) for v in e.get("values", [])}
            ti = {d["date"]: float(d["adjClose"]) for d in tiingo.get(t, []) if d.get("adjClose") is not None}
            common = sorted(set(td) & set(ti))
            if common:
                diffs = [abs(td[d] - ti[d]) / ti[d] for d in common]
                adj_diff[t] = {"common_days": len(common), "median_rel_diff": statistics.median(diffs), "max_rel_diff": max(diffs)}
    say(f"  adjusted close, Twelve Data adjust=all against Tiingo adjClose: {adj_diff}")
    rec["adjusted_agreement"] = adj_diff

    # agreement of raw closes
    agree = {}
    for t in ALL:
        td = {r_["date"]: float(r_["close"]) for r_ in series.get(t, [])}
        ti = {r_["date"]: float(r_["close"]) for r_ in tiingo.get(t, []) if r_.get("close") is not None}
        common = sorted(set(td) & set(ti))
        if common:
            diffs = [abs(td[d] - ti[d]) / ti[d] for d in common]
            agree[t] = {"common_days": len(common), "median_rel_diff": round(statistics.median(diffs), 6), "max_rel_diff": round(max(diffs), 6)}
    flagged = {t: a for t, a in agree.items() if a["max_rel_diff"] > 0.005}
    say(f"  raw close agreement with Tiingo: {len(agree)} tickers compared; above 0.5% on some day: {flagged}")
    rec["raw_agreement"] = agree

    probes = [
        ("/quote", {"symbol": "AAPL"}, 1), ("/price", {"symbol": "AAPL"}, 1), ("/eod", {"symbol": "AAPL"}, 1),
        ("/earliest_timestamp", {"symbol": "XLRE", "interval": "1day"}, 1), ("/symbol_search", {"symbol": "XLRE"}, 1),
        ("/stocks", {"symbol": "IONQ"}, 1), ("/etfs", {"symbol": "XLK"}, 1),
        ("/analyst_ratings/light", {"symbol": "AAPL"}, 1),
    ]
    for path, params, cost in probes:
        try:
            r = call(path, params, cost)
        except Stop:
            break
        entry = {"status": r.status_code}
        try:
            body = r.json()
            entry["shape"] = shape(body)
            if isinstance(body, dict) and "code" in body:
                entry["code"] = body.get("code")
                entry["message"] = scrub(str(body.get("message"))[:200])
        except ValueError:
            entry["message"] = scrub(r.text[:150])
        rec["endpoints"][path] = entry
        say(f"     probe {path}: HTTP {r.status_code} {entry.get('code', '')} {entry.get('message', '')}")

    r = call("/api_usage", {}, 1)
    usage_after = r.json() if r.status_code == 200 else {}
    say(f"  usage after: { {k: v for k, v in usage_after.items() if not isinstance(v, (dict, list))} }")
    say(f"  credits counted by the script: {spent} of 120 (free plan: 8 a minute, 800 a day); rate headers seen: {sorted(rec['rate_headers_seen'])}")
    rec["credits_counted_by_script"] = spent
    rec["usage_before"], rec["usage_after"] = usage_before, usage_after
    rec["rate_headers_seen"] = sorted(rec["rate_headers_seen"])
    rec["pass"] = ok
    summary["twelvedata"] = rec
    say(f"  Twelve Data {'PASS' if ok else 'FAIL'}")
    return ok


def main() -> None:
    say("versions:", {"httpx": version("httpx")}, "today:", TODAY, "window from", START)
    for name, k in KEYS.items():
        if not k:
            sys.exit(f"{name}: key missing in the environment")
    results = {}
    series: dict[str, list[dict]] = {}
    for name, fn in [("Tiingo", lambda: run_tiingo()), ("Twelve Data", None)]:
        try:
            if name == "Tiingo":
                results[name], series = fn()
            else:
                results[name] = run_twelvedata(series)
        except Stop as e:
            say(f"  {name}: stopped, {e}")
            results[name] = False
        except Exception as e:  # noqa: BLE001
            import traceback
            say(traceback.format_exc())
            results[name] = False
    if OUT:
        with open(os.path.join(OUT, "run_summary.json"), "w") as f:
            json.dump(summary, f, indent=1, default=str)
    say("summary:", {k: ("PASS" if v else "FAIL") for k, v in results.items()}, "(terms: the user's verdict)")
    say("RESULT: PASS" if all(results.values()) else "RESULT: FAIL")


if __name__ == "__main__":
    main()
