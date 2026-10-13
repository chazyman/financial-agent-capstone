# Design overwrites

Each entry records a change to `PROPOSAL.md` or `DESIGN.md` that the user approved and that did one of three things: overturned a design invariant, removed a rule found invalid, or reinstated a removed rule. Newest at the bottom. The file is only added to, and a rollback never undoes it.

<!-- Entry formats:

An invariant overturned:

## YYYY-MM-DD · short title
- Invariant before: the line as it was
- Invariant now: the line as it is
- Changed: the DESIGN / PROPOSAL sections

A rule found invalid:

## YYYY-MM-DD · Removed: the rule
- Rule: its name and its section
- Evidence: the test, the assumption check or the review decision that showed it was invalid
- Slices that cite it: the list
- Last commit that held it: the hash
- Changed: the sections it was removed from

A removed rule that holds again:

## YYYY-MM-DD · Reinstated: the rule
- Rule: its name and its section
- Why it holds again: the evidence
- Restored from: the commit its removal entry names
- Changed: the sections it was restored to

-->

## 2026-10-09 · Self-learning is no longer listed as out of scope
- Invariant before: **Out of scope:** portfolio management, time-series forecasting, audio, self-learning, productionization into an agent harness (`PROPOSAL.md` §1).
- Invariant now: **Out of scope:** portfolio management, time-series forecasting, audio, productionization into an agent harness (`PROPOSAL.md` §1).
- Changed: `PROPOSAL.md` §4.5 and `docs/DESIGN.md` §12.1 no longer say self-learning is not in scope. It stays a later extension the run record is kept ready for (`docs/DESIGN.md` §9, "Built to be extended", and §12.1).

## 2026-10-13 · Removed: Stooq and Stockanalysis as sources
- Rule: Stooq as the source of `prices` and Stockanalysis as the source of equity fundamentals in `financial_facts` (`docs/DESIGN.md` §1 coverage table, §10 `sources.yaml`; `PROPOSAL.md` §3.2).
- Evidence: assumption check 8 (2026-10-12, `spikes/phase0/README.md`). Stooq's `robots.txt` disallows every crawler and its CSV address answers a JavaScript verification page. Stockanalysis' terms forbid bots, scripts and browser automation, and its Download buttons have no file address. The user chose Tiingo (primary) and Twelve Data (fallback), both on free tiers, for prices. Equity facts of record come from SEC XBRL only; no source of equity fundamentals replaces Stockanalysis.
- Slices that cite it: none by name. Slice 1.14 rests on check 8.
- Last commit that held it: `46c278d`.
- Changed: `docs/DESIGN.md` §1 (coverage table, Identity bullet), §3 and §4 (the chart-estimate rules, the record), §5 (`verify_chart` and `extract_records` rows), §7 (`prices` and `financial_facts` rows, the `known_at` rule), §9.1 (diagram), §10 (`sources.yaml` templates, `source_truth`); `PROPOSAL.md` §1, §2, §3.2, §4 (diagrams) and §5 (Phase 2). How the two price providers are called (fallback, freshness, quota, symbols, adjusted close) waits for check 16 and its study.
