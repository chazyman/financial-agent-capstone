# Design Notes: Financial AI Agent Assistant

Companion to [PROPOSAL.md](../PROPOSAL.md). The proposal covers what the capstone builds and why; this document holds the implementation detail: coverage rules, acquisition, pipeline rules, agents, tools and skills, the data model, project structure, runtime and infrastructure, configuration and the detailed evaluation checks. Section references like "PROPOSAL §4.1" point to the proposal; "§5" points within this document.

---

## 1. Instruments and data coverage

`data/raw/ListEquities.csv` defines the universe. Every row is one instrument:

| Column | Example | Purpose |
|---|---|---|
| `ticker` | `XLK`, `NVDA` | Canonical key for the instrument across every source |
| `name` | Technology Select Sector SPDR Fund | Display name; also seeds the alias list used to tag news |
| `type` | `ETF` / `Equity` | Decides which sources and tools apply (table below) |
| `sector` | Information Technology | GICS sector, used for filtering and grouping |

<details>
<summary><b>What each instrument type gets</b> (sources and tools for Equity vs ETF)</summary>

| Source / capability | Equity | ETF |
|---|---|---|
| SEC filings (10-K / 10-Q / 8-K): documents → chunks; XBRL facts → `financial_facts` | Yes | No (ETFs don't file 10-Ks or 10-Qs) |
| IR decks, earnings-call transcripts | Yes | No |
| Fund documents (fact sheet, prospectus, annual report PDFs from the issuer) | No | Yes, parsed as fund-level documents; fund fields (expense ratio, AUM, …) become `financial_facts` rows; holdings tables are not expanded |
| Price CSVs (Stooq) → `prices`; fundamentals CSVs (Stockanalysis) → `financial_facts` | Prices + fundamentals | Prices only |
| TipRanks analyst ratings / consensus | Yes | Yes (ETF-level data only) |
| News (press releases + Exa) | Yes | Yes (fund- and sector-level news) |
| `verify_claim` against filings | Yes | No; claims about an ETF are marked unverifiable against filings |

</details>

**Joining data from many websites.** Filings, CSVs, news and ratings for the same instrument come from different sites, so they are normalized at ingestion time:
- **Identity.** One canonical key per instrument: its ticker from `ListEquities.csv`. Equities also store their CIK (from the SEC's ticker→CIK mapping) for filing lookups. CIK can't be the key because all 11 SPDR sector ETFs share one trust CIK. Each site's symbol format (e.g. Stooq's `aapl.us`) and the instrument's name aliases map onto the ticker.
- **Periods (equities).** Fiscal years differ (e.g. Apple's ends in September). Cross-company comparisons align on trailing-twelve-months or calendar quarters, and state which one they used.
- **Units and currency.** Values are normalized to a common unit (USD, millions), with the original value and unit kept for citation.

## 2. Data acquisition

Acquisition is the only agent-driven step of ingestion. In Phase 5 the Data Aggregator's LLM chooses, per instrument, which paths to use and which web searches to run (guided by the `source-selection` skill, §5); in Phases 1–4, and in the worker's scheduled refresh, no agent runs: acquisition follows the coverage table in §1. It then passes what it fetched to the deterministic ingestion sub-graph (§3) with `ingest`.

**Where sources live.** `config/sources.yaml` (§10) is the source catalog: URL templates per instrument type, filled from the `instruments` row (ticker, CIK, per-site symbol), plus per-instrument overrides such as an equity's investor-relations page. The acquisition tools resolve a source name through it, so adding an instrument needs a CSV row and, for an equity, one override line.

Four acquisition paths:

1. **Download path**: follow a page's own "download" button/link and fetch the underlying PDF/CSV/XLSX directly.
2. **Web search and public scraping path**: `search_web` queries the [Exa](https://exa.ai) search API (already in the course material via `MCP_Exa_tools`) for public documents and pages, and custom scraping code fetches pages with no downloadable file. Exa can't sign in, so it is used for public content only. Search is acquisition only: results are ingested into the corpus before anything can cite them, and no answer quotes a live web result.
3. **MCP path**: for sources that publish an MCP server (TipRanks), call its tools and receive structured data instead of HTML.
4. **Authenticated scraping path**: for subscriber-only pages the MCP server does not cover, a real browser session logged in with the user's account.

### TipRanks: MCP server first, Playwright fallback

- **MCP server (primary).** The `tipranks_mcp` tool calls the server through `langchain-mcp-adapters` and maps each result to `analyst_ratings` rows. The server's own tools are not exposed to the agent, so every MCP call goes through one typed tool; the `tipranks-access` skill (§5) tells the Data Aggregator which server tool to call for what. Data arrives structured (ratings, price targets, dates), so a site redesign can't break parsing. The token is read from `.env` (`TIPRANKS_MCP_TOKEN`). The exact server tools and fields are listed with `list_tools` in Phase 0 and mapped to `analyst_ratings` (§7) in Phase 2.
- **Playwright (fallback for gaps).** Covers subscriber pages or fields not available through MCP. Playwright runs a real browser locally and logs in with the account's email and password, so credentials never go to a third-party service. It stays off (`scraping.tipranks_scrape_fallback: false`) until TipRanks' terms of use have been checked; a paid account does not automatically permit automated scraping.

**Security.**
- The token, the password and the Playwright session file are secrets: they live in `.env` / `storage_state.json`, both git-ignored.
- They are redacted from Langfuse traces and never passed into an LLM prompt. Agents call the `tipranks_mcp` and `scrape_authenticated` tools, which handle authentication; skill folders never contain credentials.
- MCP output and scraped pages are untrusted data and get the same prompt-injection handling as news (§6).

## 3. Ingestion rules

These are the rules behind the ingestion diagram (PROPOSAL §4.1), taken from the course's Day 2 notebooks (`DAY_2_HANDS_ON_multimodal_finance_assistant`, `session_02_Multimodal_RAG_Pipeline`).

- **Ingestion is a deterministic sub-graph.** Everything after acquisition (§2) runs as a LangGraph sub-graph in `pipelines/ingestion/` with a fixed control flow. Models are called only at fixed steps (OCR, chart classification and reading, table re-reads, news sentiment, safety screening of untrusted text); no agent decides what happens between acquisition and storage. The reference material those steps use (filing sections, metric names, fund fields, chart prompts, table-agreement rules) lives with the sub-graph, not in skills, because no agent loads it.

- **Parse into typed elements, not a text dump.** Docling returns each table, picture, header and paragraph with its page and bounding box, and associates captions from layout rather than from publisher-specific rules. The same code therefore runs unchanged on a 10-K, an IR deck and a fund fact sheet.
- **OCR routing is a measurement, so it lives in code.** A page whose recovered text is below `parsing.ocr_min_chars` goes to LightOnOCR. Every page in the range is counted, including pages that produced nothing, so an empty scanned page can't silently disappear.
- **Crop, don't send the page.** Tables and charts are cropped by their bounding box before a vision model sees them. A page often holds several charts, and a model asked about the whole page can confidently read the wrong one.
- **Each element gets the representation it needs.** Tables become Markdown pipes, which models reason over better than JSON. Charts where exact values matter become a JSON series. Trend charts become a search caption that deliberately contains no numbers. Pictures that aren't charts are kept as `docling_picture` chunks when Docling associated a caption, otherwise dropped and counted in the trace. Picture classification is the one routing decision given to a model, because it is a judgement about content.
- **A number read from a chart is an estimate.** It is labelled `vlm_chart_json` with a confidence and never written to `financial_facts`. It becomes quotable only when a matching series is found in `financial_facts` or a fundamentals CSV, and the answer then cites that record, not the chart.
- **Tables are verified by agreement.** Docling's parsed cells and the VLM's reading of the same crop fail in different ways. A value counts as agreed only when it appears on the row whose label it claims, not merely somewhere in the table. Filing tables are also checked against the SEC's XBRL facts for the same period, a third reading that needs no model. The result is recorded as `verification_status`.
- **Charts past the cap stay as plain pictures.** After `charts.max_enriched_per_doc` pictures, the rest are stored as `docling_picture` chunks when captioned (crop + Docling's caption, no model call): retrievable, never quotable. The trace records how many were skipped, so nothing disappears silently.
- **Visual elements are embedded as crop + text together.** This narrows, but can't close, the gap between text and image vectors; retrieval handles the rest (§4).

## 4. Retrieval and verification rules

These are the rules behind the retrieval diagram (PROPOSAL §4.2).

- **Predicates come before similarity.** No similarity score can express "not allowed", so a filter such as `require_checkable` is applied before scoring. As a ranking penalty it could be outweighed by a question worded close to a chart's caption. With pgvector the predicates are a SQL `WHERE` clause in the same query as the similarity search (§7).
- **Modality normalisation.** In Day 2, text queries scored consistently closer to text chunks than to images, so tables and charts sank below the top-k on geometry alone. Scores are standardised within text and within visual chunks before merging, and the reranker settles the final order. Scores then stop being absolute, so the sufficiency gate counts candidates instead of thresholding a score.
- **Typed records skip ranking.** `record_lookup` results are exact matches, not similarity results. They join the evidence next to the reranked chunks and count toward the sufficiency gate.
- **The provenance rule.** A claim that states a number must cite a checkable source: a chunk with a second possible source (`docling_text`, `docling_table` or `lighton_ocr`) or a typed record (`record_ref`, §7). A chart may support a claim about **shape**, never about **magnitude**. The model's own `magnitude` / `shape` label is cross-checked by scanning the claim text for numbers.
- **Two policy levels, named in every answer.** `policy.enforce_provenance` requires that a second source *exists*; `policy.require_agreement` is the strict level: a magnitude claim must cite a table chunk whose check *ran and agreed*, or a typed record. A `financial_facts` row read from a table must itself be `agreed`; XBRL facts, CSV rows, `prices` and `analyst_ratings` are the record. A figure found only in prose or OCR text must be redrafted to cite its record, or it is blocked. The answer footer names the level used, because "verified" means whatever the policy made it mean.
- **One redraft, then stop.** When `verify_claims` fails, the draft goes back to the drafting step (the Financial Analyst in Phase 5) with the failure reason, up to `policy.max_draft_retries` times. The redraft passes through the same check, and a further failure stops the run with the reason. A `check_safety` failure stops at once and is never redrafted. The retry appears in the Langfuse trace and in the answer's policy footer, so it is a visible retry, not a silent repair.
- **Every loop is bounded.** The Researcher rewrites a failed query at most `retrieval.max_query_rewrites` times, and the supervisor graph runs at most `policy.max_supervisor_steps` steps (LangGraph's recursion limit). Reaching either stops the run with its reason.

## 5. Agents, tools and skills

The course's harness separates **state / skills / tools / policy** (Day 1, reinforced in Day 4). This project uses two layers:

- **Tools** are typed Python functions in `src/findoc_agent/tools/`. Each has a fixed input/output contract and is unit-tested. Tools that call a model (e.g. `classify_chart`, `route`, `draft_claims`) load their prompt from a template file, never an inline string: ingestion prompts live in `pipelines/ingestion/` (§3), the others in `prompts/`. Agents call them, and so do skill scripts. Every control is a tool (design rules below).
- **Skills** are folders in `skills/`, in the Agent Skills format: a `SKILL.md` with YAML frontmatter (`name`, `description`) followed by step-by-step instructions, optional `scripts/` that call tools, and optional `references/`. An agent starts with only the name and description of each skill it may use, and loads the full `SKILL.md` when a task needs it.

Every agent is an LLM loop plus an allowlist of tools and skills, declared in `agents/registry.py`. Adding an agent means assigning it existing tools and skills, not rewriting them. The agents' purposes are in PROPOSAL §4.3. Phase 5 wraps the Phase 1–4 pipelines rather than replacing them: the Data Aggregator hands acquired files to the ingestion sub-graph, and the Researcher drives the retrieval sub-graph.

<details>
<summary><b>Stage → owner → tools / skills</b></summary>

| Stage | Owner | Tools | Skills |
|---|---|---|---|
| Acquisition (PROPOSAL §4.1 ①) | Data Aggregator | `search_web`, `fetch_document`, `scrape_page`, `fetch_news`, `tipranks_mcp`, `scrape_authenticated`, `ingest` | `source-selection`, `tipranks-access` |
| Ingestion (PROPOSAL §4.1 ②–⑥) | No agent: deterministic sub-graph | `parse_document`, `route_ocr`, `build_chunk`, `classify_chart`, `extract_chart_series`, `caption_chart`, `verify_table`, `verify_chart`, `resolve_entity`, `dedupe`, `classify_news`, `extract_records`, `check_safety`, `embed` | none |
| Retrieval (PROPOSAL §4.2 ①–⑤): filings and fundamentals | Researcher | `search`, `record_lookup`, `rerank`, `check_sufficiency` | `retrieval-planning` |
| Retrieval: news and analyst ratings | Market Intelligence | `news_window`, `analyst_consensus`, `news_trend`, `verify_claim` | `news-digest` |
| Draft (PROPOSAL §4.2 ⑥) | Financial Analyst | `draft_claims`, `compare_periods`, `compare_companies`, `extract_metric`, `price_trend` | `financial-analysis`, `grounded-answering` |
| Scope and delegate | Supervisor | `route`, `check_freshness` | none |
| Input and output gates (PROPOSAL §4.3; `verify_claims` is §4.2 ⑦) | No agent: fixed nodes of the supervisor graph | `check_safety`, `verify_claims` | none |

</details>

<details>
<summary><b>Tools table</b> (37 tools)</summary>

| Tool | What it does | Backing module | Used by |
|---|---|---|---|
| `search_web(query, since)` | Exa search for public documents and pages; returns candidate URLs with titles and dates. Results are untrusted data and become citable only after ingestion | acquisition | Data Aggregator |
| `fetch_document(source)` | Follow a page's download link, or fetch a configured source URL; save the PDF/CSV/XLSX/JSON | acquisition | Data Aggregator |
| `scrape_page(url)` | Pull public page content when there is no downloadable file | acquisition | Data Aggregator |
| `tipranks_mcp(tool, args)` | Call a TipRanks MCP server tool (token auth) and map the result to `analyst_ratings` rows | acquisition | Data Aggregator |
| `scrape_authenticated(url)` | Fetch a subscriber-only page with the saved Playwright session; log in again only if the session has expired | acquisition | Data Aggregator |
| `fetch_news(ticker, since)` | Collect candidate news articles and press releases for a ticker since a date | acquisition | Data Aggregator |
| `ingest(items)` | Run the ingestion sub-graph on acquired items: inline for the `staleness.inline` types (the answer waits), queued to the worker for documents | acquisition | Data Aggregator |
| `parse_document(file)` | Docling conversion (via the `models` service, §9) into typed elements with page, bbox and associated caption; exports Markdown + JSON | parsing | Ingestion sub-graph |
| `route_ocr(doc)` | Count recovered characters per page (every page, including empty ones); send pages below `parsing.ocr_min_chars` to LightOnOCR-2-1B on the `models` service | parsing | Ingestion sub-graph |
| `build_chunk(element)` | Wrap one element in the metadata envelope (§7); tables as Markdown, charts with their crop | parsing | Ingestion sub-graph |
| `classify_chart(crop)` | VLM decides data-critical chart, trend chart or not a chart, plus chart type, dual axis and units | multimodal | Ingestion sub-graph |
| `extract_chart_series(crop)` | VLM reads a data-critical chart into a JSON series with a confidence; tagged `vlm_chart_json` (an estimate) | multimodal | Ingestion sub-graph |
| `caption_chart(crop)` | VLM writes searchable alt-text for a trend chart, without numbers; tagged `vlm_caption` | multimodal | Ingestion sub-graph |
| `verify_table(table)` | Compare Docling's parsed cells with the VLM's reading of the crop, matching by row label (and, for filing tables, with the XBRL facts for the same period); sets `verification_status` | verification | Ingestion sub-graph |
| `verify_chart(series, ticker)` | Compare a chart's estimated series with `financial_facts` / fundamentals CSV when a matching series exists | verification | Ingestion sub-graph |
| `resolve_entity(name_or_symbol)` | Map a site-specific symbol, CIK or name alias to the canonical ticker | extraction | Ingestion sub-graph |
| `dedupe(item)` | Drop repeat copies of the same story/document (canonical URL, content hash, near-duplicate title) | extraction | Ingestion sub-graph |
| `classify_news(item)` | Tag event type and sentiment (financial-phrasebank classifier on the `models` service, §9) | news | Ingestion sub-graph |
| `extract_records(parsed_doc)` | Turn XBRL facts, table cells, CSV rows and ratings into typed rows, carrying extraction method and verification status | extraction | Ingestion sub-graph |
| `embed(items)` | Joint text + image embedding via Gemini Embedding 2 on OpenRouter (`openrouter.data_collection: deny`) | embedding | Ingestion sub-graph |
| `search(query, predicates, k)` | One pgvector query: predicates (ticker, doc type, as-of/date window, `require_checkable`; `injection_suspect` chunks always excluded) as `WHERE`, similarity as `ORDER BY`; then modality normalisation | retrieval | Researcher |
| `record_lookup(filters)` | Exact lookup of typed records (`financial_facts`, `prices`), each returned with its `record_ref` | retrieval | Researcher |
| `rerank(query, candidates)` | Qwen3-VL-Reranker-2B on the `models` service, one document at a time: crop for visual chunks, text for prose; falls back to text, then to retrieval order | retrieval | Researcher |
| `check_sufficiency(hits)` | Stop if fewer than `retrieval.min_hits` candidates (reranked chunks plus exact record matches) survive; counts candidates, never thresholds a score | retrieval | Researcher |
| `news_window(ticker, start, end, event_types)` | Date-bounded, deduplicated news retrieval ranked by recency and source tier | news | Market Intelligence |
| `analyst_consensus(ticker, window)` | Rating distribution, median/mean price target, dispersion, upgrades/downgrades in window, implied upside vs last close | news | Market Intelligence |
| `news_trend(ticker, windows)` | Per window (`trends.windows`): news count, mean sentiment and counts by event type; change in median price target and net upgrades vs the previous window of the same length | news | Market Intelligence |
| `verify_claim(claim, ticker)` | Check a numeric news claim against filings via `record_lookup`; returns verified / contradicted / unverifiable (always unverifiable for ETFs) | news | Market Intelligence |
| `extract_metric(records, field)` | Read one metric out of typed records | analysis | Financial Analyst |
| `compare_periods(records)` | Period-over-period comparison within one company | analysis | Financial Analyst |
| `compare_companies(profiles, metrics)` | Side-by-side table across companies, on aligned fiscal periods | analysis | Financial Analyst |
| `price_trend(ticker, windows)` | From `prices`, per window (`trends.windows`): return %, return relative to the sector ETF (equities), high/low, annualised volatility, max drawdown, average daily volume and its ratio to the 3-month average; plus distance from the 52-week high/low and from the `trends.moving_averages`. Cites the price rows used as a `record_ref` date range | analysis | Financial Analyst |
| `draft_claims(question, evidence)` | Draft the answer as a strict Pydantic `Draft` of `Claim`s (text, `chunk_id` or `record_ref`, `magnitude` / `shape`), no defaults; evidence tagged CHECKABLE or ESTIMATED | analysis | Financial Analyst |
| `route(question)` | Classify scope (single company / comparison), decide whether an exact figure is needed, build the plan | harness | Supervisor |
| `check_freshness(tickers, data_types)` | Compare `fetched_at` against `staleness` thresholds; list what needs a refresh, split into inline types and queued documents; queued items are named in the answer footer as "data as of" | harness | Supervisor |
| `verify_claims(draft, evidence)` | Provenance rule (§4): every claim cites a retrieved chunk or record; a magnitude claim must cite a checkable source (under `policy.require_agreement`, an agreed table chunk or a typed record); a `shape` claim containing a number is blocked | guardrails | Output gate |
| `check_safety(text)` | Prompt-injection / unsafe-content check: deterministic rules first (known injection patterns, instructions aimed at the agent, requests for credentials), then a guard model on OpenRouter (`models.guard`) | guardrails | Input and output gates; ingestion sub-graph (untrusted text) |

</details>

<details>
<summary><b>Skills table</b> (6 skill folders)</summary>

| Skill | Teaches the agent | Scripts (tools they call) | References | Loaded by |
|---|---|---|---|---|
| `source-selection` | Starting from the configured sources (`config/sources.yaml`); preferring company-issued documents; phrasing and date-bounding web searches; permitted sources only | none | `search_guide.md` | Data Aggregator |
| `tipranks-access` | MCP first; mapping MCP fields to `analyst_ratings`; when the Playwright fallback may run | `sync_ratings.py` (`tipranks_mcp`, `scrape_authenticated`) | `field_mapping.md` | Data Aggregator |
| `retrieval-planning` | Turning a sub-query into predicates: which doc types answer which questions, when to set `require_checkable`, which as-of date applies | none | `doc_type_guide.md` | Researcher |
| `news-digest` | Source tiers, date windows, dedupe, consensus, checking news numbers against filings; produces a `NewsDigest` | `build_digest.py` (`news_window`, `analyst_consensus`, `news_trend`, `verify_claim`) | `source_tiers.md`, `event_types.md` | Market Intelligence |
| `financial-analysis` | Metric definitions (e.g. FCF margin, max drawdown), period alignment, trend windows, units, comparing equities vs ETFs | `compare.py` (`compare_periods`, `compare_companies`, `price_trend`) | `metric_definitions.md` | Financial Analyst |
| `grounded-answering` | Writing claims that pass verification: one figure per claim, cite a `chunk_id` or `record_ref`, label `magnitude` vs `shape`, never quote a number from an ESTIMATED element | none (enforcement is the output gate's `verify_claims`) | `provenance_policy.md` | Financial Analyst |

</details>

**How skills are loaded.** LangGraph with OpenRouter models has no built-in skill runtime, so `agents/skill_loader.py` provides one:
1. At startup it reads the frontmatter of every folder in `skills/` and adds the name and description of each allowed skill to that agent's system prompt.
2. `load_skill(name)` returns the full `SKILL.md`; `read_skill_reference(name, file)` returns one reference file.
3. `run_skill_script(name, script, args)` runs a script from that skill's `scripts/` folder. Only scripts inside a skill the agent may use can run, and they reach data only through tools.
4. Skills load only from the repository, never from fetched content, so a scraped page can't plant a skill.

**Design rules.**
- **Numbers are computed in code; the LLM narrates.** Consensus and comparisons are deterministic tools, so every figure can be reproduced and unit-tested.
- **Code for measurements, a model for judgements.** OCR routing, sufficiency and the provenance rule are code. Only questions with no measurable answer, such as "is this chart data-critical?", go to a model.
- **Controls are tools, never skill instructions.** OCR routing, the sufficiency gate and `verify_claims` run the same way whether or not a skill is loaded. A skill can explain the provenance rule so drafts pass it, but `verify_claims` enforces it regardless.
- **Be tolerant with heuristics, strict with controls.** Chart-routing schemas default every field, so a partial answer still routes. The `Claim` schema the verifier operates on defaults nothing.
- **Guardrails are graph nodes, not tool calls.** The input and output gates (PROPOSAL §4.3) are fixed edges of the supervisor graph, and ingestion screening (§6) is a fixed step of the ingestion sub-graph, so no LLM can skip them. The redraft and loop limits are in §4.

## 6. Market intelligence workflow

**Source tiers**, in decreasing order of authority:
1. **Company-issued**: 8-K current reports and IR press releases. The facts of record.
2. **Earnings-call transcripts**: management's own words, forward-looking and unaudited.
3. **Analyst ratings**: TipRanks. Opinions, stored as structured ratings, not as facts.
4. **Third-party news articles**: via Exa. Secondary coverage, often duplicated.

**Ingestion** (daily per ticker in the worker, or inline when a question finds the data stale; step 1 is the Data Aggregator, steps 2–5 the ingestion sub-graph):
1. **Acquire.** `fetch_news(ticker, since)` collects candidates from each tier. Ratings come from `tipranks_mcp` as structured fields; `scrape_authenticated` fills only what MCP doesn't cover, and every row records which path produced it.
2. **Resolve and filter.** `resolve_entity` tags each item to a ticker using an alias list (e.g. "Alphabet" / "Google" / `GOOGL`). Items that mention the company only in passing are dropped via a relevance score.
3. **Dedupe.** `dedupe` removes the same story syndicated across outlets, keeping the highest-tier copy and recording the others as corroborating sources.
4. **Classify.** `classify_news` tags event type (earnings, guidance, M&A, rating change, management change, legal/regulatory, product) and sentiment.
5. **Store.** Each item becomes a `news_items` row plus embedded chunks carrying `published_at`; `source_tier` and `event_type` are joined from `documents` and `news_items` (§7). Ratings become `analyst_ratings` rows; rating changes are kept as history, so the workflow can report "2 upgrades in the last 30 days".

**Retrieval and synthesis** (Market Intelligence):

```mermaid
flowchart TB
    Q["Question<br/>e.g. What happened with MSFT this month,<br/>and what do analysts think?"]
    W["news_window<br/>date filter + recency decay + source-tier rank"]
    C["analyst_consensus<br/>deterministic stats over analyst_ratings"]
    V["verify_claim<br/>numeric news claims vs 8-K / 10-Q records"]
    D["NewsDigest<br/>events, consensus, verified vs unverified claims"]
    Q --> W --> V --> D
    Q --> C --> D
```

- **Time is a first-class filter.** Every news query has an explicit window (`news.default_window_days`, overridden by the question when it states one). Ranking combines relevance with recency decay and source-tier weights, so a stale article never outranks a fresh 8-K.
- **Consensus is computed, not generated.** Buy/Hold/Sell counts, median and mean price target, dispersion and changes within the window; implied upside is measured against the last close in `prices`.
- **News claims are checked against filings.** "Revenue beat by 5%" is checked against typed records from the 8-K/10-Q and marked verified, contradicted or unverifiable (not yet filed). Contradictions are shown to the user, not silently resolved.
- **Output contract.** A Pydantic `NewsDigest`: ticker, window, events (date, type, headline, sentiment, source, url), consensus, trends per window (`news_trend`), verified claims, unverified claims.

**Guardrails.**
- Scraped news and analyst pages are the largest prompt-injection surface (Day 4 / OWASP ASI). Their text is passed to the LLM as quoted data, never as instructions, and links inside it are never followed automatically. At ingestion, `check_safety` screens all untrusted text (web pages, news, MCP output); a flagged chunk is stored with `injection_suspect` set and `search` never returns it.
- Only sources the project has permission to use are ingested.
- Answers that include ratings or sentiment carry a "not investment advice" note.

## 7. Data model

One PostgreSQL database holds everything. Typed records live in relational tables, and chunks live in a `chunks` table whose `embedding` column is a pgvector vector. Retrieval predicates are a `WHERE` clause on the same query as the similarity search, and chunks join typed records directly on `instrument_id` and `doc_id`. Schema changes go through Alembic migrations. A claim cites a typed record by `record_ref` (table + row id, e.g. `financial_facts:1234`; for a figure computed over a price series, the date range, e.g. `prices:NVDA:2026-07-01..2026-09-30`), the way it cites a chunk by `chunk_id`.

**Exact search first.** The starting corpus is small (19 instruments), so `search` starts with exact (sequential) similarity search. That guarantees the predicates are applied exactly. An approximate HNSW index is added only when the corpus grows. With an index, pgvector filters the candidates the index returns, so a selective predicate can return fewer than `top_k` rows; pgvector's iterative index scans (0.8+) address that.

| Table | Key columns | Written by | Read by |
|---|---|---|---|
| `chunks` | chunk_id, doc_id, instrument_id, the envelope fields below, `embedding vector(1536)` | Ingestion sub-graph | Researcher, Market Intelligence |
| `instruments` | `instrument_id` (ticker), name, type (ETF / Equity), sector, cik (equities only), aliases, per-site symbols | Ingestion sub-graph, from `ListEquities.csv` | all |
| `documents` | doc_id, instrument_id, source, url, doc_type, source_tier, published_at, fetched_at, content_hash | Ingestion sub-graph | Researcher, Market Intelligence, Supervisor (freshness) |
| `financial_facts` | fact_id, instrument_id, doc_id, metric, value, unit, period, fiscal/calendar alignment, cell reference, extraction_method (xbrl / docling_table / csv), verification_status. Equity figures from SEC XBRL data, filing tables and fundamentals CSVs; ETF fund fields from fund documents | Ingestion sub-graph | Researcher, Financial Analyst |
| `prices` | price_id, instrument_id, date (unique with instrument_id), open, high, low, close, volume, source, fetched_at. The first load brings `prices.history_days` of history; each refresh inserts or updates on (instrument_id, date) | Ingestion sub-graph (Stooq CSVs) | Financial Analyst (`price_trend`), Market Intelligence (implied upside), Researcher, Supervisor (freshness) |
| `news_items` | doc_id, instrument_id, headline, event_type, sentiment, published_at, corroborating_sources | Ingestion sub-graph | Market Intelligence |
| `analyst_ratings` | rating_id, instrument_id, firm, analyst, action, rating, price_target, rating_date, source, acquisition_path (mcp / scrape) | Ingestion sub-graph | Market Intelligence |

**Chunk metadata envelope.** Follows the Day 2 `Chunk` model. These are columns of `chunks`, so retrieval predicates and the verifier can decide whether a number taken from a chunk may be quoted.

<details>
<summary><b>Chunk envelope fields</b> (13 rows)</summary>

| Field | Meaning |
|---|---|
| `chunk_id`, `doc_id`, `instrument_id` | Identity; `doc_id` joins to `documents` |
| `kind` | `text` / `table` / `chart` / `picture` |
| `docling_label`, `section`, `caption` | What Docling called it, the section it sits under, and the caption it associated |
| `page`, `bbox` | Where it is; with `doc_id` this forms the citation anchor a reviewer can open |
| `data_as_of`, `published_at` | Two different dates, kept apart. "Information as of" drives as-of predicates; the publication date drives recency |
| `representation` | What is embedded and shown to the model: text, Markdown table, chart JSON or caption |
| `image_path` | The cropped element, for tables and charts |
| `extraction_method` | `docling_text`, `docling_table`, `docling_picture`, `lighton_ocr`, `vlm_chart_json` or `vlm_caption` |
| `confidence` | 1.0 for parsed text and cells; lower for OCR and chart reads |
| `content_hash` | Hash of the representation, so a cited chunk can be shown to be unchanged |
| `injection_suspect` | Set at ingestion when `check_safety` flags untrusted text (§6); retrieval excludes these chunks with a `WHERE` clause |
| `verification_status` | `not_attempted` / `agreed` / `disagreed` / `failed`: whether a second extraction *ran*, and what it said |
| `has_second_source` (derived) | Whether a second extraction is *possible*: true for `docling_text`, `docling_table` and `lighton_ocr`, false for picture and chart extractions (`docling_picture`, `vlm_chart_json`, `vlm_caption`) |

</details>

## 8. Project structure

- **Separation of concerns.** Acquisition, parsing, retrieval, agents and evaluation are separate modules, mirroring the course's split of harness components (state, skills, tools, policy).
- **Notebooks stay thin.** Each notebook imports from the package and exercises it; no business logic lives in a notebook.
- **Config- and schema-driven.** The universe (`ListEquities.csv`), settings and secrets (§10) and Pydantic schemas are data, so new instruments, sources or record types need no code changes.
- **One implementation for single- and multi-agent.** Tools are grouped by domain, not by agent, because some are shared (Market Intelligence's `verify_claim` calls the Researcher's `record_lookup`). The Phase 5 agents wrap the Phase 1–4 sub-graphs; nothing is re-implemented. The provenance gate is one sub-graph (`pipelines/answer.py`) used by both the Phase 3 answer graph and the Phase 5 output gate.
- **Testable units.** Each tool module is tested on its own (e.g. a parser against one document), matching the course's emphasis on step-level inspection.

```
financial-agent-capstone/
├── PROPOSAL.md
├── README.md
├── pyproject.toml         # uv project: package dependencies + groups dev, models
├── uv.lock
├── compose.yaml           # postgres, redis, worker, models (§9.1)
├── .env.example           # secret and per-machine values (§10); real .env and
│                          # storage_state.json are git-ignored
├── config/
│   ├── settings.yaml      # all non-secret settings (§10)
│   └── sources.yaml       # source catalog: URL templates per type + per-instrument overrides (§2)
├── docker/
│   ├── app.Dockerfile     # worker image (§9.1)
│   └── models.Dockerfile  # CUDA image: Docling, LightOnOCR, reranker, sentiment classifier
├── data/
│   ├── raw/               # downloaded PDFs, CSVs, ListEquities.csv
│   └── processed/         # Docling Markdown/JSON artifacts and element crops
├── docs/
│   └── DESIGN.md          # this document
├── migrations/            # Alembic migrations for PostgreSQL + pgvector
├── services/
│   └── models/            # small internal HTTP server on the GPU: /parse, /ocr, /rerank, /sentiment
├── skills/                # skill folders: SKILL.md + scripts + references
├── src/
│   └── findoc_agent/      # Python package: tools, agents, pipelines, worker
├── tests/                 # unit tests per tool module; each skill script run on a fixture document
└── notebooks/             # 01–06, the deliverable demos (PROPOSAL §5); scratch/ is
                           # git-ignored throwaway exploration
```

<details>
<summary><b><code>src/findoc_agent/</code> in detail</b></summary>

```
src/findoc_agent/
├── config.py              # Settings: .env + settings.yaml + sources.yaml (§10)
├── worker.py              # arq worker: scheduled refresh + queued document refreshes
├── models_client.py       # HTTP client for the models service (/parse, /ocr, /rerank, /sentiment)
├── schemas/               # Pydantic models: Chunk envelope, chart/table schemas,
│                          # Claim/Draft, NewsDigest, typed records
├── stores/                # PostgreSQL access (SQLAlchemy + pgvector): chunks and typed records
├── prompts/               # prompt templates for model-calling tools outside ingestion (route, draft_claims)
├── tools/                 # every tool in §5: typed input/output, no inline prompt
│   ├── acquisition.py     # search_web, fetch_document, scrape_page, fetch_news,
│   │                      # tipranks_mcp, scrape_authenticated, ingest
│   ├── parsing.py         # parse_document (Docling), route_ocr, build_chunk
│   ├── multimodal.py      # classify_chart, extract_chart_series, caption_chart
│   ├── verification.py    # verify_table, verify_chart
│   ├── extraction.py      # extract_records, resolve_entity, dedupe
│   ├── embedding.py       # embed
│   ├── retrieval.py       # search, record_lookup, rerank, check_sufficiency
│   ├── news.py            # classify_news, news_window, analyst_consensus, news_trend, verify_claim
│   ├── analysis.py        # extract_metric, compare_periods, compare_companies, price_trend, draft_claims
│   ├── harness.py         # route, check_freshness
│   └── guardrails.py      # verify_claims, check_safety
├── agents/                # LangGraph agents: a prompt + an allowlist of tools and skills
│   ├── registry.py        # agent → allowed tools and skills
│   ├── skill_loader.py    # reads skill frontmatter; load_skill, read_skill_reference, run_skill_script
│   ├── supervisor.py      # supervisor graph: input gate; output gate = pipelines/answer.py + check_safety
│   ├── data_aggregator.py
│   ├── researcher.py
│   ├── market_intelligence.py
│   └── financial_analyst.py
├── pipelines/             # sub-graphs composed from tools
│   ├── ingestion/         # deterministic ingestion sub-graph + its prompts and reference data
│   ├── retrieval.py       # retrieval sub-graph (PROPOSAL §4.2 ①–⑤)
│   └── answer.py          # provenance gate: draft → verify_claims → one redraft, with a pluggable
│                          # drafting node (draft_claims in Phase 3, the Financial Analyst in Phase 5)
└── eval/                  # Ragas harness + multimodal regression suite (§11)
```

</details>

<details>
<summary><b><code>skills/</code> in detail</b> (6 skill folders)</summary>

```
skills/
├── source-selection/       SKILL.md, references/search_guide.md
├── tipranks-access/        SKILL.md, scripts/sync_ratings.py, references/field_mapping.md
├── retrieval-planning/     SKILL.md, references/doc_type_guide.md
├── news-digest/            SKILL.md, scripts/build_digest.py, references/source_tiers.md, event_types.md
├── financial-analysis/     SKILL.md, scripts/compare.py, references/metric_definitions.md
└── grounded-answering/     SKILL.md, references/provenance_policy.md
```

A `SKILL.md` starts with frontmatter that is always visible to the agent; the body is loaded only on demand:

```markdown
---
name: source-selection
description: Choose which sources to acquire for an instrument and which web searches to run, by instrument type and staleness. Use before any acquisition.
---
1. Start from the instrument's configured sources; read references/search_guide.md before any web search ...
```

</details>

## 9. Runtime and infrastructure

**One package, several entry points.** `findoc_agent`, the AI engine, holds all the logic as tools and LangGraph graphs. The notebooks, the ingestion worker and the tests all import it. Each phase is demoed in a thin notebook, in the course's style, but nothing a notebook proves is re-implemented elsewhere.

**Productionization.** Moving the engine into an agent harness compatible with LangGraph is outside the capstone's scope, and its design is still to be defined. This section covers what the capstone runs.

### 9.1 Services

```mermaid
flowchart TB
    NB["Notebooks and tests<br/>WSL2 uv environment, findoc_agent graphs"]
    RQ[("redis<br/>job queue")]
    W["worker<br/>arq: scheduled + queued document ingestion"]
    M["models (NVIDIA GPU)<br/>Docling /parse, LightOnOCR /ocr,<br/>Qwen3-VL-Reranker /rerank,<br/>sentiment classifier /sentiment"]
    PG[("postgres<br/>pgvector chunks + typed records")]
    OR["OpenRouter<br/>agent LLM, vision model, embeddings"]
    EXT["Sources<br/>SEC, IR, SPDR, Stooq, Exa, TipRanks MCP"]
    LF["Langfuse Cloud<br/>traces"]

    NB -->|enqueue refresh| RQ --> W
    NB --> PG
    W --> PG
    NB -->|parse, OCR, sentiment, rerank| M
    NB -->|inline refresh| EXT
    W -->|parse, OCR, sentiment| M
    W --> EXT
    NB --> OR
    W --> OR
    NB -.-> LF
    W -.-> LF
```

`docker compose up -d` runs the four services below. Notebooks and tests run outside Docker, in the WSL2 `uv` environment, and reach the services on `localhost`.

<details>
<summary><b>Compose services</b> (4)</summary>

| Service | Image | Role |
|---|---|---|
| `worker` | `docker/app.Dockerfile`: Python + `uv`, the package's locked dependencies (no `dev` or `models` group) and `src/` (no `skills/`: no agent runs in the worker), non-root user; command `arq` | Scheduled refresh per `refresh.*`, acquiring from `config/sources.yaml` with no agent. Also runs the filing and fund-document refreshes queued when a question finds them stale; prices, news and ratings refresh inline in the supervisor graph |
| `models` | `docker/models.Dockerfile` (CUDA, torch, Docling, transformers) | The only GPU user: `/parse` (Docling), `/ocr` (LightOnOCR-2-1B), `/rerank` (Qwen3-VL-Reranker-2B), `/sentiment` (news sentiment classifier). A small internal HTTP server that serves models, not users. Loads each model on first use. On the RTX 3080 Ti (16 GB) all four stay loaded (~8–10 GB; the sentiment classifier adds little); `models_service.idle_unload_seconds` enables the Day 2 unload-when-idle pattern on smaller GPUs |
| `postgres` | `pgvector/pgvector:pg17` | Chunks with embeddings, typed records; Alembic migrations |
| `redis` | `redis:7` | Job queue only |

</details>

Why a separate `models` service:
- It keeps CUDA and `torch` (several GB) out of the `uv` environment and the worker image, so both stay light and install quickly.
- It gives the GPU a single owner that manages memory, whether the caller is a notebook or the worker.
- It can move to a remote GPU by changing `MODELS_URL`.

### 9.2 Development

- **Environment:** Windows 11, WSL2, Docker Desktop (WSL2 backend) and VS Code with the WSL extension. Clone the repo inside WSL2, not under `/mnt/c`, so file access stays fast. `uv sync` creates the Python environment (the package plus `dev`, uv's default group); nothing is installed on Windows. The NVIDIA GPU reaches the `models` container through the WSL2 driver; check it once with `nvidia-smi` in a CUDA container.
- **Loop per feature:**
  1. Explore in a throwaway notebook in `notebooks/scratch/` (git-ignored); delete it once the code is promoted.
  2. Promote the working code into `tools/` with a test.
  3. Wire it into a graph.
  4. Demo it in the phase notebook and check its Langfuse trace.
- **Dependencies:** `uv` with one lockfile. The package's own dependencies are in `[project.dependencies]` (also what the worker image installs), plus two groups: `dev` (Jupyter, debugpy, pytest, ruff, nbstripout) and `models` (GPU image only).
- **Tests:** pytest markers `integration` (needs the compose services), `gpu` (needs the `models` service) and `network` (external sites and APIs). Plain `pytest` runs only the unmarked unit tests; `pytest -m integration` and the others opt in.
- **Notebooks in git:** outputs are stripped on commit (`nbstripout` as a git filter), so diffs show code only. Seeing results means running the notebook.
- **Tracing:** from Phase 1, every graph run, in a notebook or the worker, sends a trace to Langfuse Cloud, with keys from `.env`.

**Debugging.**

| What | How |
|---|---|
| Notebooks | VS Code Jupyter with the `uv` environment as kernel and `%autoreload 2`. Run `jupyter lab` if you prefer the course's JupyterLab |
| Graphs and tools | VS Code debugger on a notebook cell or a test; the Langfuse trace of each run |
| Worker | `docker compose logs worker`; to step through ingestion, stop the container and run `arq` from the `uv` environment under the debugger |
| Tests, migrations | Test Explorer or `pytest`; `alembic upgrade head` |
| `models` | `docker compose logs models` |

## 10. Configuration

No setting is hardcoded in Python or notebooks. Every value comes from one of three files:

| File | Holds | In git? |
|---|---|---|
| `.env` | Secrets and per-machine values: API keys, passwords, database, Redis and models-service URLs | No. Only `.env.example` (names, empty values) is committed |
| `config/settings.yaml` | Everything else: refresh cadence, staleness thresholds, parsing, retrieval, news and policy settings, model choices, rate limits, cost caps | Yes |
| `config/sources.yaml` | The source catalog (§2): URL templates per instrument type and per-instrument overrides | Yes |

All three are loaded at startup into one typed Pydantic `Settings` object (`pydantic-settings`). A missing or mistyped value fails at startup rather than midway through a run. Environment variables override YAML. Code refers only to keys (e.g. `settings.staleness.news`), never to literal values.

<details>
<summary><b><code>.env.example</code></b></summary>

```bash
OPENROUTER_API_KEY=
EXA_API_KEY=
SEC_USER_AGENT=                             # "Name email@example.com", required by SEC's fair-access policy
TIPRANKS_MCP_TOKEN=
TIPRANKS_EMAIL=
TIPRANKS_PASSWORD=
LANGFUSE_PUBLIC_KEY=
LANGFUSE_SECRET_KEY=
LANGFUSE_HOST=https://cloud.langfuse.com    # or the US region host, per your Langfuse Cloud account
POSTGRES_USER=
POSTGRES_PASSWORD=
POSTGRES_DB=findoc
# URLs as seen from WSL (notebooks, tests); compose.yaml gives the worker service-name URLs
DATABASE_URL=postgresql://user:password@localhost:5432/findoc
REDIS_URL=redis://localhost:6379/0
MODELS_URL=http://localhost:8001
```

</details>

<details>
<summary><b><code>config/settings.yaml</code></b> (proposed starting values)</summary>

```yaml
universe:
  file: data/raw/ListEquities.csv

refresh:                     # how often scheduled ingestion runs, per data type
  filings: 7d
  fund_documents: 7d
  prices: 1d
  news: 1d
  analyst_ratings: 1d

staleness:                   # past these ages, check_freshness triggers a refresh
  filings: 7d
  fund_documents: 7d
  prices: 1d
  news: 1d
  analyst_ratings: 1d
  inline: [prices, news, analyst_ratings]   # refreshed before answering; documents are queued to the worker

news:
  default_window_days: 30
  recency_half_life_days: 7
  source_tier_weights: {company_issued: 1.0, transcript: 0.8, analyst: 0.7, third_party: 0.5}

parsing:
  ocr_min_chars: 400                # per page; below this, LightOnOCR recovers the page
  images_scale: 2.0                 # Docling crop resolution

charts:
  max_enriched_per_doc: 20          # pictures per document sent to the VLM; the rest stay docling_picture chunks (§3)

embedding:
  model: google/gemini-embedding-2
  dimensions: 1536                  # 128-3072 on this model; keep <= 2000 so a pgvector HNSW index stays possible
  joint_image_text: true            # embed crops together with their text

retrieval:
  top_k: 8
  modality_normalisation: true
  rerank: true
  rerank_model: Qwen/Qwen3-VL-Reranker-2B
  min_hits: 1
  max_query_rewrites: 2             # Researcher retries after a failed sufficiency gate, then reports what is missing
  exact_search: true                # sequential scan until the corpus needs an HNSW index (§7)

models_service:
  idle_unload_seconds: 0            # 0 = keep models loaded. RTX 3080 Ti laptop (16 GB) holds all four models
                                    # together (~8-10 GB); set e.g. 600 on a smaller GPU
  dtype: bfloat16                   # native on Ampere (RTX 30xx)

policy:
  enforce_provenance: true          # magnitude claims must cite a chunk with a second source
  require_agreement: false          # strict level: only agreed tables or typed records (§4)
  max_draft_retries: 1              # redrafts after a failed provenance check before the run stops (§4)
  max_supervisor_steps: 25          # LangGraph recursion limit for the supervisor graph (§4)

scraping:
  requests_per_minute: 20
  tipranks_scrape_fallback: false   # stays off until TipRanks' terms are checked

openrouter:
  data_collection: deny             # every chat and embedding call routes only to providers that don't retain payloads

models:
  primary: <openrouter model id>
  fallback: <openrouter model id>
  vision: qwen/qwen3-vl-32b-instruct     # chart/table reading, as in Day 2
  ocr: lightonai/LightOnOCR-2-1B         # local, GPU
  guard: <openrouter model id>           # check_safety's model stage, after its rules
  sentiment: <financial-phrasebank classifier id>   # local, GPU

prices:
  history_days: 365                 # first load; later refreshes insert or update on (instrument_id, date)

trends:
  windows: [1w, 1m, 3m]             # default windows for price_trend and news_trend
  moving_averages: [50, 200]        # trading days

cost:
  max_usd_per_query: 0.50

normalization:
  currency: USD
  unit: millions
```

</details>

<details>
<summary><b><code>config/sources.yaml</code></b> (shape; URLs to be filled in Phase 1)</summary>

```yaml
templates:                   # per instrument type; {placeholders} come from the instruments row
  Equity:
    sec_submissions: https://data.sec.gov/submissions/CIK{cik10}.json
    sec_companyfacts: https://data.sec.gov/api/xbrl/companyfacts/CIK{cik10}.json
    prices: https://stooq.com/q/d/l/?s={stooq_symbol}&i=d
    fundamentals: <stockanalysis URL pattern>
  ETF:
    fund_documents: <SPDR fund page pattern>
    prices: https://stooq.com/q/d/l/?s={stooq_symbol}&i=d

overrides:                   # per instrument
  AAPL: {ir_page: https://investor.apple.com}
```

</details>

## 11. Evaluation checks in detail

- **Retrieval and answer quality (Phase 1+):** a hand-labelled QA set (question, expected chunk or record, expected answer) that grows each phase.
  - From Phase 1: retrieval hit rate, i.e. whether the expected source is in the top `retrieval.top_k`.
  - From Phase 4: Ragas precision/recall, with synthetic questions added and the earlier phases' graphs re-run so scores stay comparable.
- **Market intelligence quality (Phase 2+)**, on a small hand-labelled sample:
  - Entity-tagging and event-type accuracy.
  - Dedupe precision/recall.
  - Window correctness: no item outside the requested date range.
  - `analyst_consensus` figures match the source pages.
- **Multimodal pipeline (Phases 1–3)**, the regression suite suggested at the end of the Day 2 notebook:
  - Table agreement rate: the share of VLM-read values confirmed on their own row by Docling's cells.
  - Picture routing accuracy (data-critical / trend / not a chart) on a hand-labelled sample.
  - OCR routing: scanned or empty pages caught, born-digital pages left alone.
  - Retrieval recall per modality, including the rank of the best table or chart with modality normalisation on vs off.
  - Provenance test: a forged numeric claim citing a chart must be blocked (after at most one redraft), and must pass only when `enforce_provenance` is switched off.
- **Comparison correctness (Phase 2+):**
  - `compare_periods` / `compare_companies` / `price_trend` unit-tested against hand-computed results.
  - Spot checks of fiscal-period alignment across companies.
- **Trajectories:** Phase 4 ranks the answer graph's paths (sufficiency stop, redraft). Phase 5 applies the same harness to the agents: right tool (retrieval vs direct answer), right skill for the task (e.g. `news-digest` for a news question, not `retrieval-planning`), and self-correction when wrong.
- **Gates and screening (Phase 5):**
  - An injection in the question stops at the input gate.
  - An instruction planted in a scraped page, news item or MCP result is flagged at ingestion and never retrieved.
  - An unsafe draft stops without a redraft.
  - A draft that fails provenance twice stops with its reason.
  - `check_safety`'s rules are unit-tested on known injection patterns, without the guard model.
