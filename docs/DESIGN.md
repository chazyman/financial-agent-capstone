# Design Notes: Financial AI Agent Assistant

Companion to [PROPOSAL.md](../PROPOSAL.md). The proposal covers what the capstone builds and why; this document holds the implementation detail: coverage rules, acquisition, pipeline rules, agents, tools and skills, the data model, project structure, runtime and infrastructure, configuration, the detailed evaluation checks, and traceability and observability. Section references like "PROPOSAL §4.1" point to the proposal; "§5" points within this document.

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

**Where sources live.** `config/sources.yaml` (§10) is the source catalog: URL templates per instrument type, filled from the `instruments` row (ticker, CIK, per-site symbol), plus per-instrument overrides such as an equity's investor-relations page, and each source's truth coefficient (§4). The acquisition tools resolve a source name through it, so adding an instrument needs a CSV row and, for an equity, one override line.

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

These are the rules behind the ingestion diagram (PROPOSAL §4.1), taken from the course's Day 2 notebooks (`DAY_2_HANDS_ON_multimodal_finance_assistant`, `session_02_Multimodal_RAG_Pipeline`). The layout rules were then checked in October 2026 by running Docling 2.132 on an 11-page third-party analyst report with two-column cards, borderless tables and vector charts; the rules that run changed are marked *(validated)*.

- **Ingestion is a deterministic sub-graph.** Everything after acquisition (§2) runs as a LangGraph sub-graph in `pipelines/ingestion/` with a fixed control flow. Models are called only at fixed steps (OCR, chart classification and reading, table re-reads, news sentiment, safety screening of untrusted text); no agent decides what happens between acquisition and storage. The reference material those steps use (filing sections, metric names, fund fields, chart prompts, table-agreement rules) lives with the sub-graph, not in skills, because no agent loads it.

- **Parse into typed elements, not a text dump.** Docling returns each table, picture, header and paragraph with its page and bounding box, and associates captions from layout rather than from publisher-specific rules. The same code therefore runs unchanged on a 10-K, an IR deck and a fund fact sheet.
- **OCR routing is a measurement, so it lives in code.** *(validated)* A page goes to LightOnOCR when its text layer holds fewer than `parsing.ocr_min_chars` characters and a raster image covers at least `parsing.ocr_min_image_fraction` of it. The count is taken on the PDF's own text layer, so table cells and text inside charts are included: counting Docling's text items alone gave 25 characters for a born-digital page that held a chart and a table. The second condition separates a scanned page from a sparse one. Every page in the range is counted, including pages that produced nothing, so an empty scanned page can't silently disappear.
- **Text chunks follow headings, and headings get a path.** *(validated)* A text chunk is a heading plus the paragraphs and list items under it, up to `chunking.max_chars`; it never crosses a heading, and a heading is never a chunk on its own. Docling returns every PDF heading at one level, so the engine infers the levels in code from the text layer (font size and weight) and stores the path in `section`, e.g. "Bearish Factors > Heavy cash burn". Without the path, a card loses the heading that gives it its meaning.
- **Furniture is not content.** *(validated)* Page headers and footers are not chunked. Docling labels most of them; a line that repeats at the same position on most pages is treated as furniture even when it is labelled otherwise.
- **Repeated cards are segmented by pattern, not by label.** *(validated)* Five identical event cards came back as one table, two dated headings and two plain paragraphs. Dated items (§6) are therefore cut on their date-and-headline pattern in the text, not on Docling's labels.
- **Text is normalised and its damage is counted.** *(validated)* Text is NFKC-normalised. Characters the PDF's fonts could not map arrive as U+FFFD, typically a non-breaking hyphen; one between two word characters is repaired to a hyphen, and the number left is stored in `ingest_runs`.
- **Crop, don't send the page.** Tables and charts are cropped by their bounding box before a vision model sees them. A page often holds several charts, and a model asked about the whole page can confidently read the wrong one.
- **A crop travels with its context.** *(validated)* A chart's title and legend often sit outside its box, so the crop alone may not say which colour is which series, and Docling associated no caption with any picture or table. Every model that receives a crop also receives the nearest heading above it, the text Docling attached to the picture, and the short text lines directly above or below it. When no caption is associated, that heading is stored as the chunk's caption.
- **Binary artifacts live on disk, named by their hash.** The parser writes each document's Docling JSON and Markdown and every crop (table, chart, picture) under `data/processed/<doc_id>/`. A crop's file name is its content hash, and a crop is never overwritten, so a refresh can't change an image an earlier answer cited. A chunk carries the crop's path and hash (§7). Graph state, records and traces carry that reference, never the bytes (§5, §12).
- **Each element gets the representation it needs.** Tables become Markdown pipes, which models reason over better than JSON. Charts where exact values matter become a JSON series. Trend charts become a search caption that deliberately contains no numbers. Pictures that aren't charts are kept as `docling_picture` chunks when Docling associated a caption, otherwise dropped and counted in `ingest_runs` (§12). Whether a chart is data-critical is the one routing decision given to a vision model, because it is a judgement about content.
- **Decorative pictures never reach the vision model.** *(validated)* Docling's own picture classifier runs with the parse, on the `models` service. A picture it labels with a class in `charts.skip_classes` (logo, icon), or whose shorter side is under `charts.min_short_side`, is dropped and counted. Only the rest go to `classify_chart` and count toward `charts.max_enriched_per_doc`. In validation this removed 17 of 21 pictures, a logo on every page and six badges, and left the four charts.
- **A number read from a chart is an estimate.** It is labelled `vlm_chart_json` with a confidence and never written to `financial_facts`. It becomes quotable only when a matching series is found in `financial_facts` or a fundamentals CSV, and the answer then cites that record, not the chart.
- **Text printed in a chart is text.** *(validated)* Docling attaches the text inside a picture's box to the picture. The titles, legends and data labels among it are stored as a text chunk linked to the chart (`docling_text`, with the chart as its `parent_chunk_id`), so a printed "IONQ (-45.78%)" is quotable with the chart as its context. Axis ticks, meaning bare numbers or dates lined up along an edge of the plot, are kept as chart metadata and are never quotable. Only values read from the bars and lines are estimates.
- **Tables are verified by agreement.** Docling's parsed cells and the VLM's reading of the same crop fail in different ways. A value counts as agreed only when it appears on the row whose label it claims, not merely somewhere in the table. Filing tables are also checked against the SEC's XBRL facts for the same period, a third reading that needs no model. The result is recorded as `verification_status`.
- **Table checks are bounded.** A filing table is checked against XBRL first; when XBRL confirms its values it is `agreed` with no vision call. The vision re-read runs only on the remaining tables whose cells become `financial_facts`, up to `tables.max_verified_per_doc` per document. Every other table keeps `not_attempted`, stays usable under the default policy (§4), and is counted in `ingest_runs`.
- **A table must look like one.** *(validated)* Checks in code run on every table Docling returns, before it is represented or verified:
  - A "table" whose median cell is longer than `tables.max_prose_cell_chars` and that has no numeric column is prose: it is demoted to text and counted.
  - A table with no header row whose columns alternate label and value is a key-value grid. It is stored as "label: value" lines, not as Markdown with its first row promoted to a header.
  - A row with a label and no values is a group label and is joined to the labels below it ("Balance Sheet > Total Debt").
  - A dash in a numeric column is a missing value, never zero and never a minus sign. Unit suffixes are parsed per cell, since one column can mix millions and billions.
- **Charts past the cap stay as plain pictures.** After `charts.max_enriched_per_doc` pictures, the rest are stored as `docling_picture` chunks when captioned (crop + Docling's caption, no model call): retrievable, never quotable. `ingest_runs` records how many were skipped, so nothing disappears silently.
- **Visual elements are embedded as crop + text together.** This narrows, but can't close, the gap between text and image vectors; retrieval handles the rest (§4).

## 4. Retrieval and verification rules

These are the rules behind the retrieval diagram (PROPOSAL §4.2).

- **Predicates come before similarity.** No similarity score can express "not allowed", so a filter such as `require_checkable` is applied before scoring. As a ranking penalty it could be outweighed by a question worded close to a chart's caption. With pgvector the predicates are a SQL `WHERE` clause in the same query as the similarity search (§7).
- **Modality normalisation.** In Day 2, text queries scored consistently closer to text chunks than to images, so tables and charts sank below the top-k on geometry alone. Scores are standardised within text and within visual chunks before merging, and the reranker settles the final order. Scores then stop being absolute, so the sufficiency gate counts candidates instead of thresholding a score.
- **Source truth weighs the order.** Every source has a coefficient from 1 to 10 in `config/sources.yaml` (§10): how far a figure from it can be trusted as a fact of record. Company filings are 10; a third-party, AI-written report is 6. After reranking, each candidate's score, on a 0 to 1 scale, is multiplied by `(source_truth / 10) ^ retrieval.source_truth_weight`; a weight of 0 switches it off. When the reranker is not used, the normalised scores are first rescaled to 0 to 1 within the candidate set, because a standardised score can be negative. This is a preference, not a rule: a much closer match from a weaker source can still rank first, which is why the next rule and the floor below exist.
- **Source truth is not extraction reliability.** `confidence` and `verification_status` say how well an element was read; source truth says how far its publisher can be trusted. A table from a third-party report can be read perfectly and still be wrong, so neither is folded into the other.
- **Typed records skip ranking.** `record_lookup` results are exact matches, not similarity results. They join the evidence next to the reranked chunks and count toward the sufficiency gate.
- **Records have precedence.** Because records skip ranking, the coefficient can't reach them there. When `record_lookup` finds the same metric and period from several sources, it returns the record from the source with the highest source truth; a tie goes to an `agreed` record, then to the most recently fetched. The others are attached to it as corroborating, or as disagreeing when they differ by more than `records.agreement_tolerance`. A disagreement is shown in the answer, never resolved silently, as for news claims (§6).
- **The drafter sees the image of a chart.** Text and table hits reach the drafting step as text or Markdown. A hit whose kind is listed in `drafting.include_crops` (charts and pictures) also sends its crop, downscaled to `drafting.crop_max_side`, so a claim about shape is made from the image and not only from a caption written before the question was known. The provenance rule below is unchanged: a number read off that crop is a magnitude claim citing an un-checkable source, and is blocked. The primary and fallback models must therefore accept images.
- **The provenance rule.** A claim that states a number must cite a checkable source: a chunk with a second possible source (`docling_text`, `docling_table` or `lighton_ocr`) or a typed record (`record_ref`, §7). A chart may support a claim about **shape**, never about **magnitude**. Text printed in a chart is a separate text chunk and follows the text rule (§3). The model's own `magnitude` / `shape` label is cross-checked by scanning the claim text for numbers.
- **Two policy levels, named in every answer.** `policy.enforce_provenance` requires that a second source *exists*; `policy.require_agreement` is the strict level: a magnitude claim must cite a table chunk whose check *ran and agreed*, or a typed record. A `financial_facts` row read from a table must itself be `agreed`; XBRL facts, CSV rows, `prices` and `analyst_ratings` are the record. A figure found only in prose or OCR text must be redrafted to cite its record, or it is blocked. The answer footer names the level used, because "verified" means whatever the policy made it mean.
- **Source truth can be a floor.** `policy.min_source_truth` is 0 by default: every source is admissible and the coefficient only weighs the order. Set higher, it becomes a hard rule when a figure is needed: sources below it are excluded by a predicate, like `require_checkable`, and `verify_claims` blocks a magnitude claim that cites one.
- **Every citation shows its source truth.** Each cited source is shown with its coefficient, e.g. "TipRanks AI analyst report, 6/10", and the answer footer names the floor in force beside the policy level.
- **One redraft, then stop.** When `verify_claims` fails, the draft goes back to the drafting step (the Financial Analyst in Phase 5) with the failure reason, up to `policy.max_draft_retries` times. The redraft passes through the same check, and a further failure stops the run with the reason. A `check_safety` failure stops at once and is never redrafted. The retry appears in the Langfuse trace and in the answer's policy footer, so it is a visible retry, not a silent repair.
- **Every loop is bounded.** The Researcher rewrites a failed query at most `retrieval.max_query_rewrites` times; past that, `retrieve` (§5) refuses the call. The supervisor graph runs at most `policy.max_supervisor_steps` steps (LangGraph's recursion limit), and each sub-agent's own loop at most `policy.max_agent_steps`. Reaching any of them stops the run with its reason, a `StopReason` (§5).

## 5. Agents, tools and skills

The course's harness separates **state / skills / tools / policy** (Day 1, reinforced in Day 4). This project uses two layers:

- **Tools** are typed Python functions in `src/finrag_engine/tools/`, each unit-tested. Every tool returns a named Pydantic model (the Returns column of the tools table). The 16 tools on an agent's allowlist also take a Pydantic arguments model, named after the tool (`retrieve` → `RetrieveArgs`) and defined in the tool's module, because that is where model-written arguments arrive. Pipeline-internal tools take Pydantic objects directly. Tools that call a model (e.g. `classify_chart`, `route`, `draft_claims`) load their prompt from a template file, never an inline string: ingestion prompts live in `pipelines/ingestion/` (§3), the others in `prompts/`. Agents call them, and so do skill scripts. Every control is a tool (design rules below).
- **Skills** are folders in `skills/`, in the Agent Skills format: a `SKILL.md`, optional `scripts/` that call tools, and optional `references/`. The `SKILL.md` follows the course's Day 1 format: YAML frontmatter (`name`, `description`, `version`, `owner`) and six required sections (assumptions, constraints, tools, procedure, verification, failure modes). An agent starts with only the name and description of each skill it may use, and loads the full `SKILL.md` when a task needs it.

Every sub-agent is an LLM loop plus an allowlist of tools and skills. It is declared as a frozen `AgentSpec` in `agents/registry.py` (name, prompt, tools, skills, model, step limit, brief and artifact types) and built by one factory, `make_agent_node` in `agents/factory.py`. Adding a sub-agent means adding a spec, not writing a graph. The Supervisor is not a loop (see "The Supervisor plans" below). The agents' purposes are in PROPOSAL §4.3. Phase 5 wraps the Phase 1–4 pipelines rather than replacing them: the Data Aggregator hands acquired files to the ingestion sub-graph with `ingest`, and the Researcher runs the retrieval sub-graph with `retrieve`. Each sub-graph runs as a whole; neither agent can call its inner steps.

<details>
<summary><b>Stage → owner → tools / skills</b></summary>

| Stage | Owner | Tools | Skills |
|---|---|---|---|
| Acquisition (PROPOSAL §4.1 ①) | Data Aggregator | `search_web`, `fetch_document`, `scrape_page`, `fetch_news`, `tipranks_mcp`, `scrape_authenticated`, `ingest` | `source-selection`, `tipranks-access` |
| Ingestion (PROPOSAL §4.1 ②–⑥) | No agent: deterministic sub-graph | `parse_document`, `route_ocr`, `build_chunks`, `classify_chart`, `extract_chart_series`, `caption_chart`, `verify_table`, `verify_chart`, `resolve_entity`, `dedupe`, `classify_news`, `extract_records`, `check_safety`, `embed` | none |
| Retrieval planning: filings and fundamentals | Researcher | `retrieve` | `retrieval-planning` |
| Retrieval (PROPOSAL §4.2 ①–⑤) | No agent: deterministic sub-graph, run by `retrieve` | `search`, `record_lookup`, `rerank`, `check_sufficiency` | none |
| Retrieval: news and analyst ratings | Market Intelligence | `news_window`, `analyst_consensus`, `news_trend`, `verify_claim` | `news-digest` |
| Draft (PROPOSAL §4.2 ⑥) | Financial Analyst | `compare_periods`, `compare_companies`, `extract_metric`, `price_trend`; its final step returns a `Draft` as structured output | `financial-analysis`, `grounded-answering` |
| Plan and delegate | Supervisor: fixed nodes, one model call (`route`) | `route`, `check_freshness` | none |
| Input and output gates (PROPOSAL §4.3; `verify_claims` is §4.2 ⑦) | No agent: fixed nodes of the supervisor graph | `check_safety`, `verify_claims` | none |

</details>

<details>
<summary><b>Tools table</b> (38 tools)</summary>

| Tool | What it does | Returns | Backing module | Used by |
|---|---|---|---|---|
| `search_web(query, since)` | Exa search for public documents and pages; returns candidate URLs with titles and dates. Results are untrusted data and become citable only after ingestion | `list[SearchResult]` | acquisition | Data Aggregator |
| `fetch_document(source)` | Follow a page's download link, or fetch a configured source URL; save the PDF/CSV/XLSX/JSON | `AcquiredItem` | acquisition | Data Aggregator |
| `scrape_page(url)` | Pull public page content when there is no downloadable file | `AcquiredItem` | acquisition | Data Aggregator |
| `tipranks_mcp(tool, args)` | Call a TipRanks MCP server tool (token auth) and map the result to `analyst_ratings` rows | `list[AnalystRating]` | acquisition | Data Aggregator |
| `scrape_authenticated(url)` | Fetch a subscriber-only page with the saved Playwright session; log in again only if the session has expired | `AcquiredItem` | acquisition | Data Aggregator |
| `fetch_news(ticker, since)` | Collect candidate news articles and press releases for a ticker since a date | `list[AcquiredItem]` | acquisition | Data Aggregator |
| `ingest(items)` | Run the ingestion sub-graph on acquired items: inline for the `staleness.inline` types (the answer waits), queued to the worker for documents | `IngestReport` | acquisition | Data Aggregator |
| `parse_document(file)` | Docling conversion (via the `models` service, §9) into typed elements with page, bbox, associated caption and Docling's picture class; exports Markdown + JSON and writes the crops | `ParsedDocument` | parsing | Ingestion sub-graph |
| `route_ocr(doc)` | Count the text-layer characters of every page, including empty ones; send a page to LightOnOCR-2-1B on the `models` service when it is below `parsing.ocr_min_chars` and mostly covered by a raster image (§3) | `ParsedDocument` | parsing | Ingestion sub-graph |
| `build_chunks(doc)` | Turn the parsed document into chunks in the metadata envelope (§7): text grouped under its heading path, one chunk per table (Markdown or key-value lines), chart and picture with its crop, and a linked text chunk for a chart's printed text. Drops furniture and decorative pictures and applies the table checks (§3) | `list[Chunk]` | parsing | Ingestion sub-graph |
| `classify_chart(crop)` | VLM decides data-critical chart, trend chart or not a chart, plus chart type, dual axis and units. Runs only on pictures that passed the decorative filter, with the crop's context (§3) | `ChartRouting` | multimodal | Ingestion sub-graph |
| `extract_chart_series(crop)` | VLM reads a data-critical chart into a JSON series with a confidence; tagged `vlm_chart_json` (an estimate) | `ChartSeries` | multimodal | Ingestion sub-graph |
| `caption_chart(crop)` | VLM writes searchable alt-text for a trend chart, without numbers; tagged `vlm_caption` | `ChartCaption` | multimodal | Ingestion sub-graph |
| `verify_table(table)` | Compare Docling's parsed cells with the XBRL facts for the same period (filing tables, first) and, where that doesn't settle it and within `tables.max_verified_per_doc`, with the VLM's reading of the crop, matching by row label; sets `verification_status` | `TableVerification` | verification | Ingestion sub-graph |
| `verify_chart(series, ticker)` | Compare a chart's estimated series with `financial_facts` / fundamentals CSV when a matching series exists | `ChartVerification` | verification | Ingestion sub-graph |
| `resolve_entity(name_or_symbol)` | Map a site-specific symbol, CIK or name alias to the canonical ticker | `Instrument` | extraction | Ingestion sub-graph |
| `dedupe(item)` | Drop repeat copies of the same story/document (canonical URL, content hash, near-duplicate title) | `DedupeResult` | extraction | Ingestion sub-graph |
| `classify_news(item)` | Tag event type and sentiment (financial-phrasebank classifier on the `models` service, §9) | `NewsClassification` | news | Ingestion sub-graph |
| `extract_records(parsed_doc)` | Turn XBRL facts, table cells, CSV rows and ratings into typed rows, carrying extraction method and verification status | `RecordBatch` | extraction | Ingestion sub-graph |
| `embed(items)` | Joint text + image embedding via Gemini Embedding 2 on OpenRouter (`openrouter.data_collection: deny`) | `list[Embedding]` | embedding | Ingestion sub-graph |
| `retrieve(sub_query, predicates)` | Run the retrieval sub-graph (PROPOSAL §4.2 ①–⑤) as a whole and return an `EvidenceSet`: the chunks and records that survived, or what is missing when the sufficiency gate fails. At most `1 + retrieval.max_query_rewrites` calls per brief; further calls are refused | `EvidenceSet` | retrieval | Researcher |
| `search(query, predicates, k)` | One pgvector query: predicates (ticker, doc type, as-of/date window, `require_checkable`, the source-truth floor; `injection_suspect` chunks always excluded) as `WHERE`, similarity as `ORDER BY`; then modality normalisation | `list[Hit]` | retrieval | Retrieval sub-graph |
| `record_lookup(filters)` | Exact lookup of typed records (`financial_facts`, `prices`): one per metric and period, chosen by source truth, with the records that corroborate or disagree attached (§4). Each is returned with its `record_ref` | `list[Hit]` | retrieval | Retrieval sub-graph; `verify_claim` |
| `rerank(query, candidates)` | Qwen3-VL-Reranker-2B on the `models` service, one document at a time: crop for visual chunks, text for prose; falls back to text, then to retrieval order. Each score is then weighed by source truth (§4) | `list[Hit]` | retrieval | Retrieval sub-graph |
| `check_sufficiency(hits)` | Stop if fewer than `retrieval.min_hits` candidates (reranked chunks plus exact record matches) survive; counts candidates, never thresholds a score | `SufficiencyResult` | retrieval | Retrieval sub-graph |
| `news_window(ticker, start, end, event_types)` | Date-bounded, deduplicated news retrieval ranked by recency and source truth | `list[NewsItem]` | news | Market Intelligence |
| `analyst_consensus(ticker, window)` | Rating distribution, median/mean price target, dispersion, upgrades/downgrades in window, implied upside vs last close | `Consensus` | news | Market Intelligence |
| `news_trend(ticker, windows)` | Per window (`trends.windows`): news count, mean sentiment and counts by event type; change in median price target and net upgrades vs the previous window of the same length | `NewsTrend` | news | Market Intelligence |
| `verify_claim(claim, ticker)` | Check a numeric news claim against filings via `record_lookup`; returns verified / contradicted / unverifiable (always unverifiable for ETFs) | `ClaimCheck` | news | Market Intelligence |
| `extract_metric(records, field)` | Read one metric out of typed records | `MetricValue` | analysis | Financial Analyst |
| `compare_periods(records)` | Period-over-period comparison within one company | `Comparison` | analysis | Financial Analyst |
| `compare_companies(profiles, metrics)` | Side-by-side table across companies, on aligned fiscal periods | `Comparison` | analysis | Financial Analyst |
| `price_trend(ticker, windows)` | From `prices`, per window (`trends.windows`): return %, return relative to the sector ETF (equities), high/low, annualised volatility, max drawdown, average daily volume and its ratio to the 3-month average; plus distance from the 52-week high/low and from the `trends.moving_averages`. Cites the price rows used as a `record_ref` date range | `PriceTrend` | analysis | Financial Analyst |
| `draft_claims(question, evidence)` | Draft the answer as a strict Pydantic `Draft` of `Claim`s (text, `source_kind`, `source_id`, `magnitude` / `shape`; §7), no defaults; evidence tagged CHECKABLE or ESTIMATED, with the crop of each chart and picture hit (§4). The Phase 5 Financial Analyst is not given this tool: it returns the same `Draft` schema, from the same drafting prompt, as its structured output | `Draft` | analysis | Phase 3 answer graph (drafting node) |
| `route(question)` | One model call that returns a typed `Plan`: scope (single company / comparison), whether an exact figure is needed, and the ordered steps with their sub-queries | `Plan` | harness | Supervisor |
| `check_freshness(tickers, data_types)` | Compare `fetched_at` against `staleness` thresholds; list what needs a refresh, split into inline types and queued documents; queued items are named in the answer footer as "data as of" | `FreshnessReport` | harness | Supervisor |
| `verify_claims(draft, evidence)` | Provenance rule (§4): every claim cites a retrieved chunk or record; a magnitude claim must cite a checkable source (under `policy.require_agreement`, an agreed table chunk or a typed record); a `shape` claim containing a number is blocked; so is a magnitude claim citing a source below `policy.min_source_truth` | `VerificationResult` | guardrails | Output gate |
| `check_safety(text)` | Prompt-injection / unsafe-content check: deterministic rules first (known injection patterns, instructions aimed at the agent, requests for credentials), then a guard model on OpenRouter (`models.guard`) | `SafetyVerdict` | guardrails | Input and output gates; ingestion sub-graph (untrusted text) |

</details>

<details>
<summary><b>Skills table</b> (6 skill folders)</summary>

| Skill | Teaches the agent | Tools declared | Script | References | Loaded by |
|---|---|---|---|---|---|
| `source-selection` | Starting from the configured sources (`config/sources.yaml`); preferring company-issued documents; phrasing and date-bounding web searches; permitted sources only | `search_web`, `fetch_document`, `scrape_page`, `fetch_news` | none | `search_guide.md` | Data Aggregator |
| `tipranks-access` | MCP first; mapping MCP fields to `analyst_ratings`; when the Playwright fallback may run | `tipranks_mcp`, `scrape_authenticated` | `sync_ratings.py` | `field_mapping.md` | Data Aggregator |
| `retrieval-planning` | Turning a sub-query into predicates: which doc types answer which questions, when to set `require_checkable`, which as-of date applies | `retrieve` | none | `doc_type_guide.md` | Researcher |
| `news-digest` | Source tiers, date windows, dedupe, consensus, checking news numbers against filings; produces a `NewsDigest` | `news_window`, `analyst_consensus`, `news_trend`, `verify_claim` | `build_digest.py` | `source_tiers.md`, `event_types.md` | Market Intelligence |
| `financial-analysis` | Metric definitions (e.g. FCF margin, max drawdown), period alignment, trend windows, units, comparing equities vs ETFs | `compare_periods`, `compare_companies`, `price_trend` | `compare.py` | `metric_definitions.md` | Financial Analyst |
| `grounded-answering` | Writing claims that pass verification: one figure per claim, cite a `chunk_id` or `record_ref`, label `magnitude` vs `shape`, never quote a number from an ESTIMATED element | none (enforcement is the output gate's `verify_claims`) | none | `provenance_policy.md` | Financial Analyst |

</details>

**The Supervisor plans; code delegates.** No model chooses the next agent. The supervisor graph is a fixed sequence:

1. **Input gate:** `check_safety` on the question.
2. **Plan:** `route` makes the Supervisor's one model call and returns a typed `Plan`: the scope, whether an exact figure is needed, and an ordered list of steps. Each step names a sub-agent from a closed set (Researcher, Market Intelligence) and carries its sub-query.
3. **Freshness:** `check_freshness` compares the plan's tickers and data types with the `staleness` thresholds, in code. Stale inline types put a Data Aggregator step ahead of the plan; stale documents are queued to the worker (§9.1).
4. **Plan walker:** a code node runs the steps in order with `Command(goto=...)`, handing each sub-agent its brief and storing the artifact it returns.
5. **Draft:** the Financial Analyst runs last, and only if a step returned evidence. Otherwise the run stops and names what is missing.
6. **Output gate:** `verify_claims` and `check_safety` (§4).

**Handoffs are typed.** Sub-agents don't share a conversation. Each receives a brief and returns an artifact, both Pydantic models in `schemas/`:

| Sub-agent | Receives | Returns |
|---|---|---|
| Data Aggregator | `AcquisitionBrief`: tickers and the stale data types from `check_freshness` | `AcquisitionReport`: what was ingested inline, what was queued, what failed |
| Researcher | `ResearchBrief`: sub-query, tickers, as-of date, whether an exact figure is needed | `EvidenceSet`: the chunks and records that survived retrieval, each tagged CHECKABLE or ESTIMATED, or what is missing |
| Market Intelligence | `NewsBrief`: tickers, date window, event types | `NewsDigest` (§6) |
| Financial Analyst | `AnalysisBrief`: the question, every `EvidenceSet` and `NewsDigest` collected, whether an exact figure is needed; on a redraft, the failure reason | `Draft` |

- **Private history.** A sub-agent's messages live in its own sub-graph and are dropped when it returns. Only the artifact enters the supervisor state; the full transcript is kept in `run_steps` and in the Langfuse trace (§12). This is what keeps news text out of the Researcher's context (PROPOSAL §4.3).
- **Hits keep their modality.** Each hit in an `EvidenceSet` carries its kind, representation, crop path and hash, anchor (page and bounding box), extraction method, verification status, and its source with that source's truth coefficient. A table or a chart reaches the Analyst as what it is, never flattened to text, and the factory builds the Analyst's message from it, crop included (§4).
- **Code fills the envelope.** The model writes only the judgement fields of a plan step (sub-query, tickers, window). Step ids and timestamps are stamped in code.
- **Only the Analyst writes its artifact.** The `Draft` is model-written, which is why it passes through `verify_claims`. The other three artifacts are assembled in code from the tool results of the run, so no model retypes evidence, a digest or an ingestion report.
- **Supervisor state** is a `TypedDict` in `agents/state.py`: the question, the `Plan`, the freshness result, one artifact per completed step, the draft attempt count and the stop reason. Artifacts are stored as JSON (design rules below).

**One path to a tool.** An agent or a skill script reaches a tool only through `call_tool` in `tools/gateway.py`, the engine's version of the Day 1 hands-on gateway.

- **Registry.** Every tool registers a `ToolSpec` in `tools/registry.py`: its name, arguments model, return model and a `read_only` flag. `ingest` is the only tool not marked read-only, and a unit test asserts it, so "ingestion is the only writer to the corpus" (§7) is checked in code.
- **Checks, in order:** the tool is registered; it is on the calling agent's allowlist (`AgentSpec.tools`); the run's tool-call budget is not exhausted; the arguments validate. Authorisation comes before validation, so a schema error never describes a tool to a caller that may not use it.
- **Every outcome is traced** (§12): completed, failed or refused, each with its reason. A refused call and a call that was never attempted must not look the same.
- **Errors are typed** (`errors.py`). A refusal raises `ToolDenied`; a failure raises `ToolError` with a failure class (transient, rate limit, bad arguments, unknown), so a caller can tell a retryable failure from a terminal one. A tool never returns an error as a string.
- **Skill scripts use the same path.** `run_skill_script` gives the script a gateway bound to the calling agent, so a script can't reach a tool its agent couldn't.
- **Sub-graphs call their tools directly.** The ingestion and retrieval sub-graphs fix the order of their steps in code, so there is nothing to authorise.

**Every run ends in a typed result.** `engine.answer()` and `engine.ask()` return a `RunResult` (`run.py`) and don't raise for an expected stop.

- **`RunResult`** holds the `Answer` or the partial result, a `StopReason`, the run id of its stored record (§12) and the trace id.
- **`StopReason`** is a closed set: `answered`, `unsafe_input`, `insufficient_evidence`, `provenance_failed`, `unsafe_output`, `budget_exhausted`, `step_limit`, `tool_failed`. LangGraph raises an error when the recursion limit is hit; the engine catches it and returns `step_limit`, so a truncated run never looks like a finished one.
- **`Budget`** is built per run from the settings: tool calls (`policy.max_tool_calls`), model calls (`policy.max_model_calls`) and cost (`cost.max_usd_per_query`). The gateway counts tool calls; the chat backend counts model calls and adds up cost from the usage each response reports. Exhausting any of them stops the run with `budget_exhausted`. The budget covers the answer path only: ingestion triggered by a question has its own per-document limits (`charts.max_enriched_per_doc`, `tables.max_verified_per_doc`) and records its model calls and cost on `ingest_runs` (§12).
- **Model fallback** belongs to the chat backend (`backends/openrouter.py`): when the primary model fails, it retries on `models.fallback` and records the switch in the trace.

**How skills are loaded.** LangGraph with OpenRouter models has no built-in skill runtime, so `agents/skill_loader.py` provides one:
1. When the supervisor graph is built, it parses every `SKILL.md` into a Pydantic `Skill` and adds the name and description of each allowed skill to that agent's system prompt. A skill that fails validation stops the build, so no question is answered with a broken skill.
2. `load_skill(name)` returns the full `SKILL.md`; `read_skill_reference(name, file)` returns one reference file.
3. `run_skill_script(name, script, args)` runs a script from that skill's `scripts/` folder. Only scripts inside a skill the agent may use can run, and they reach data only through the tool gateway.
4. Skills load only from the repository, never from fetched content, so a scraped page can't plant a skill.

These three functions are registered tools like any other: they take arguments models and pass through the gateway.

**What the `Skill` model checks.** The format and most of the rules come from the Day 1 hands-on:
- **Shape.** All six sections are present, each as a bullet list; the description is long enough to act as the trigger.
- **Tools.** Every tool under `## Tools` is registered and on the allowlist of the agent that loads the skill, and the procedure names no tool the skill didn't declare. The list may be empty for a skill that only guides writing (`grounded-answering`).
- **Files.** Every script and reference the skill names exists in its folder.
- **Failure modes** are written `when -> then`, and at least one ends in a stop. A stop that ends the whole run names its `StopReason`, and the loader rejects a name outside that set; any other outcome names the artifact field it is reported in.

A skill's verification and failure-mode sections tell the agent how to report a condition. They enforce nothing: the stop itself is always a code path (design rules below).

**Design rules.**
- **Numbers are computed in code; the LLM narrates.** Consensus and comparisons are deterministic tools, so every figure can be reproduced and unit-tested.
- **Code for measurements, a model for judgements.** OCR routing, sufficiency and the provenance rule are code. Only questions with no measurable answer, such as "is this chart data-critical?", go to a model.
- **Controls are tools, never skill instructions.** OCR routing, the sufficiency gate and `verify_claims` run the same way whether or not a skill is loaded. A skill can explain the provenance rule so drafts pass it, but `verify_claims` enforces it regardless.
- **No agent holds a control.** The sufficiency gate, `verify_claims` and `check_safety` are steps of a sub-graph or a gate, never entries in an agent's allowlist. An agent reaches a pipeline only as a whole, through `ingest` or `retrieve`.
- **Be tolerant with heuristics, strict with controls.** Chart-routing schemas default every field, so a partial answer still routes. The `Claim` schema the verifier operates on defaults nothing.
- **State holds JSON; nodes re-validate.** Every graph state (ingestion, retrieval, answer, supervisor) is a `TypedDict` of JSON-serialisable values, as in the course notebooks. A node stores a Pydantic object as `model_dump(mode="json")`, and the node that reads it validates it again, so a control such as `verify_claims` never trusts an object it did not parse. The state also stays safe for any checkpointer. Binary and large artifacts never enter a state: page images, crops, the Docling document and embeddings stay on disk or in PostgreSQL, and the state carries a path or id with a hash (§3).
- **Guardrails are graph nodes, not tool calls.** The input and output gates (PROPOSAL §4.3) are fixed edges of the supervisor graph, and ingestion screening (§6) is a fixed step of the ingestion sub-graph, so no LLM can skip them. The redraft and loop limits are in §4.

## 6. Market intelligence workflow

**Source tiers**, in decreasing order of authority:
1. **Company-issued**: 8-K current reports and IR press releases. The facts of record.
2. **Earnings-call transcripts**: management's own words, forward-looking and unaudited.
3. **Analyst ratings**: TipRanks. Opinions, stored as structured ratings, not as facts.
4. **Third-party news articles**: via Exa. Secondary coverage, often duplicated.

Each tier's weight is the source-truth coefficient of its sources (§4), set in `config/sources.yaml`.

**Ingestion** (daily per ticker in the worker, or inline when a question finds the data stale; step 1 is the Data Aggregator, steps 2–5 the ingestion sub-graph):
1. **Acquire.** `fetch_news(ticker, since)` collects candidates from each tier. Ratings come from `tipranks_mcp` as structured fields; `scrape_authenticated` fills only what MCP doesn't cover, and every row records which path produced it.
2. **Resolve and filter.** `resolve_entity` tags each item to a ticker using an alias list (e.g. "Alphabet" / "Google" / `GOOGL`). Items that mention the company only in passing are dropped via a relevance score.
3. **Dedupe.** `dedupe` removes the same story syndicated across outlets, keeping the highest-tier copy and recording the others as corroborating sources.
4. **Classify.** `classify_news` tags event type (earnings, guidance, M&A, rating change, management change, legal/regulatory, product) and sentiment.
5. **Store.** Each item becomes a `news_items` row plus embedded chunks carrying `published_at`; `source_tier` and `event_type` are joined from `documents` and `news_items` (§7). A document that holds several dated items, such as a report's list of corporate events, produces one row per item, each with its own date. Ratings become `analyst_ratings` rows; rating changes are kept as history, so the workflow can report "2 upgrades in the last 30 days".

**Retrieval and synthesis** (Market Intelligence):

```mermaid
flowchart TB
    Q["Question<br/>e.g. What happened with MSFT this month,<br/>and what do analysts think?"]
    W["news_window<br/>date filter + recency decay + source-truth rank"]
    C["analyst_consensus<br/>deterministic stats over analyst_ratings"]
    V["verify_claim<br/>numeric news claims vs 8-K / 10-Q records"]
    D["NewsDigest<br/>events, consensus, verified vs unverified claims"]
    Q --> W --> V --> D
    Q --> C --> D
```

- **Time is a first-class filter.** Every news query has an explicit window (`news.default_window_days`, overridden by the question when it states one). Ranking combines relevance with recency decay and source truth (§4), so a stale article never outranks a fresh 8-K.
- **Consensus is computed, not generated.** Buy/Hold/Sell counts, median and mean price target, dispersion and changes within the window; implied upside is measured against the last close in `prices`.
- **News claims are checked against filings.** "Revenue beat by 5%" is checked against typed records from the 8-K/10-Q and marked verified, contradicted or unverifiable (not yet filed). Contradictions are shown to the user, not silently resolved.
- **Output contract.** A Pydantic `NewsDigest`: ticker, window, events (date, type, headline, sentiment, source, url), consensus, trends per window (`news_trend`), verified claims, unverified claims.

**Guardrails.**
- Scraped news and analyst pages are the largest prompt-injection surface (Day 4 / OWASP ASI). Their text is passed to the LLM as quoted data, never as instructions, and links inside it are never followed automatically. At ingestion, `check_safety` screens all untrusted text (web pages, news, MCP output); a flagged chunk is stored with `injection_suspect` set and `search` never returns it.
- Only sources the project has permission to use are ingested.
- Answers that include ratings or sentiment carry a "not investment advice" note.

## 7. Data model

One PostgreSQL database holds everything. Typed records live in relational tables, and chunks live in a `chunks` table whose `embedding` column is a pgvector vector. Retrieval predicates are a `WHERE` clause on the same query as the similarity search, and chunks join typed records directly on `instrument_id` and `doc_id`. Schema changes go through Alembic migrations. A claim cites a typed record by `record_ref` (table + row id, e.g. `financial_facts:1234`; for a figure computed over a price series, the date range, e.g. `prices:NVDA:2026-07-01..2026-09-30`), the way it cites a chunk by `chunk_id`.

**A claim's source is flat for the model, typed for the verifier.** In a `Claim` the model writes two required fields: `source_kind` (`chunk` / `record`) and `source_id` (a `chunk_id` or a `record_ref`). A validator parses them into a typed `ChunkRef` or `RecordRef` (table, then a row id or a ticker and date range), so a malformed reference fails at the schema, before `verify_claims` runs. Flat fields hold up better than a union in structured output across OpenRouter providers and on the fallback model.

**ORM stays inside `stores/`.** SQLAlchemy models are private to `stores/`. Store classes accept and return the Pydantic schemas in `schemas/`, so tools and agents never hold a session or an ORM row. Each table is therefore defined twice, as an ORM model and as a schema; that duplication is the price of the boundary.

**Source truth is configuration, not data.** A source's coefficient (§4) is not a column. It is resolved from `config/sources.yaml` through the `source` of a document or record when a query runs, so changing a coefficient needs no re-ingestion. The value in force is copied only into `run_evidence`, so an old answer still shows the coefficient it was given under.

**Exact search first.** The starting corpus is small (19 instruments), so `search` starts with exact (sequential) similarity search. That guarantees the predicates are applied exactly. An approximate HNSW index is added only when the corpus grows. With an index, pgvector filters the candidates the index returns, so a selective predicate can return fewer than `top_k` rows; pgvector's iterative index scans (0.8+) address that.

**Two groups of tables.** The corpus tables below are written only by the ingestion sub-graph. The record tables after them are written only by the engine's `Tracer` and the evaluation harness (§12). No tool and no agent writes to either group. Every chunk and every typed record carries the `ingest_run_id` of the ingestion run that produced it.

| Table | Key columns | Written by | Read by |
|---|---|---|---|
| `chunks` | chunk_id, doc_id, instrument_id, the envelope fields below, `embedding vector(1536)` | Ingestion sub-graph | Researcher, Market Intelligence |
| `instruments` | `instrument_id` (ticker), name, type (ETF / Equity), sector, cik (equities only), aliases, per-site symbols | Ingestion sub-graph, from `ListEquities.csv` | all |
| `documents` | doc_id, instrument_id, source, url, doc_type, source_tier, published_at, fetched_at, content_hash | Ingestion sub-graph | Researcher, Market Intelligence, Supervisor (freshness) |
| `financial_facts` | fact_id, instrument_id, doc_id, metric, value, unit, period, fiscal/calendar alignment, cell reference, extraction_method (xbrl / docling_table / csv), verification_status. Equity figures from SEC XBRL data, filing tables and fundamentals CSVs; ETF fund fields from fund documents | Ingestion sub-graph | Researcher, Financial Analyst |
| `prices` | price_id, instrument_id, date (unique with instrument_id), open, high, low, close, volume, source, fetched_at. The first load brings `prices.history_days` of history; each refresh inserts or updates on (instrument_id, date) | Ingestion sub-graph (Stooq CSVs) | Financial Analyst (`price_trend`), Market Intelligence (implied upside), Researcher, Supervisor (freshness) |
| `news_items` | news_id, doc_id, item index (unique with doc_id: one document can hold several dated items), instrument_id, headline, event_type, sentiment, source_sentiment (the publisher's own tag, when it has one), published_at, corroborating_sources | Ingestion sub-graph | Market Intelligence |
| `analyst_ratings` | rating_id, instrument_id, firm, analyst, action, rating, price_target, rating_date, source, acquisition_path (mcp / scrape). A rating a source generates with a model, such as TipRanks' AI Analyst, is stored like any other, with the product's name as the firm | Ingestion sub-graph | Market Intelligence |

<details>
<summary><b>Record tables</b> (5 tables; rules in §12)</summary>

| Table | Key columns | Written by | Read by |
|---|---|---|---|
| `ingest_runs` | ingest_run_id, doc_id, started_at, status; parser, OCR, vision and embedding model ids; prompt versions; settings hash; counts (pages, OCR pages, chunks by kind, charts enriched and skipped, tables verified, not attempted and demoted to text, pictures dropped as decorative or uncaptioned, unmapped characters, records written); model calls, tokens and cost; trace_id | Ingestion sub-graph | Evaluation, debugging |
| `runs` | run_id, scenario_id, entry point (`answer` / `ask`), question, plan, answer, stop_reason, policy level, versions (git commit, settings hash, prompt, model and skill versions), tokens, cost, latency, trace_id, created_at | `Tracer` | Evaluation, replay |
| `run_evidence` | run_id, source (`chunk_id` or `record_ref`), the representation as it was read, content_hash, the crop's path and hash for a table, chart or picture, anchor, CHECKABLE / ESTIMATED, the source-truth coefficient in force, whether a claim cited it | `Tracer` | Replay, evaluation |
| `run_steps` | run_id, step index, parent step, agent, step type (plan step / model call / tool call / gate), input and output as JSON in chat-message form (an image as its path and hash, never its bytes), outcome and reason, tokens, cost, latency, Langfuse observation id | `Tracer` | Evaluation, trajectory export |
| `run_scores` | run_id, name, value, scorer and its version, comment, created_at; append-only | Evaluation harness, `Tracer` (gate results) | Evaluation |

</details>

**Chunk metadata envelope.** Follows the Day 2 `Chunk` model. These are columns of `chunks`, so retrieval predicates and the verifier can decide whether a number taken from a chunk may be quoted.

<details>
<summary><b>Chunk envelope fields</b> (15 rows)</summary>

| Field | Meaning |
|---|---|
| `chunk_id`, `doc_id`, `instrument_id` | Identity; `doc_id` joins to `documents` |
| `ingest_run_id` | The ingestion run that produced the chunk; joins to `ingest_runs` for the model and prompt versions used (§12) |
| `kind` | `text` / `table` / `chart` / `picture` |
| `docling_label`, `section`, `caption` | What Docling called it, the heading path it sits under (levels inferred in code, §3), and the caption Docling associated, or the nearest heading when it associated none |
| `parent_chunk_id` | For the text printed in a chart: the chart chunk it belongs to (§3). Empty otherwise |
| `page`, `bbox` | Where it is; with `doc_id` this forms the citation anchor a reviewer can open |
| `data_as_of`, `published_at` | Two different dates, kept apart. "Information as of" drives as-of predicates; the publication date drives recency |
| `representation` | What is embedded and shown to the model: text, Markdown table, chart JSON or caption |
| `image_path`, `image_hash` | The cropped element, for tables, charts and pictures, and the hash of that file (§3) |
| `extraction_method` | `docling_text`, `docling_table`, `docling_picture`, `lighton_ocr`, `vlm_chart_json` or `vlm_caption` |
| `confidence` | 1.0 for parsed text and cells; lower for OCR and chart reads |
| `content_hash` | Hash of the representation, so a cited chunk can be shown to be unchanged |
| `injection_suspect` | Set at ingestion when `check_safety` flags untrusted text (§6); retrieval excludes these chunks with a `WHERE` clause |
| `verification_status` | `not_attempted` / `agreed` / `disagreed` / `failed`: whether a second extraction *ran*, and what it said |
| `has_second_source` (derived) | Whether a second extraction is *possible*: true for `docling_text`, `docling_table` and `lighton_ocr`, false for picture and chart extractions (`docling_picture`, `vlm_chart_json`, `vlm_caption`) |

</details>

## 8. Project structure

- **Separation of concerns.** Acquisition, parsing, retrieval, agents and evaluation are separate modules, mirroring the course's split of harness components (state, skills, tools, policy).
- **Notebooks stay thin.** Each notebook calls the package through its `Engine` (below); no business logic lives in a notebook.
- **Config- and schema-driven.** The universe (`ListEquities.csv`), settings and secrets (§10) and Pydantic schemas are data, so new instruments, sources or record types need no code changes.
- **One implementation for single- and multi-agent.** Tools are grouped by domain, not by agent, because some are shared (Market Intelligence's `verify_claim` calls `record_lookup`, a step of the retrieval sub-graph). The Phase 5 agents wrap the Phase 1–4 sub-graphs; nothing is re-implemented. The provenance gate is one sub-graph (`pipelines/answer.py`) used by both the Phase 3 answer graph and the Phase 5 output gate.
- **Testable units.** Each tool module is tested on its own (e.g. a parser against one document), matching the course's emphasis on step-level inspection.

**Classes and functions.** The package follows the course notebooks: functions do the work, and a few small classes hold state or a resource.

- **Tools and graph nodes are functions.** A tool takes its arguments and the engine context and returns a Pydantic model; a node takes the state and returns a partial update. There are no tool classes, node classes or agent base classes.
- **Classes hold state or a resource:** `Settings`, the stores, the backend clients, `SkillLoader`, `Tracer` and `Engine`. Each receives its dependencies through its constructor.
- **`EngineContext`** (`context.py`) is a frozen dataclass of the settings, the stores, the backends and the tracer. It is built once and reaches every graph through LangGraph's `context_schema` and every tool as an explicit parameter; nothing reads a module-level global. The agent factory binds it when it exposes a tool to a model, so the context never appears in the schema the model sees.
- **`Engine`** (`engine.py`) is the composition root and the only public API. `Engine.from_settings()` builds the context, and each graph is compiled once, on first use, so the worker, which only ingests, never loads agents or skills. Callers use `engine.ingest()`, `engine.refresh()` (config-driven acquisition plus ingestion, used by the worker), `engine.retrieve()`, `engine.answer()` (the Phase 1–4 answer graph) and `engine.ask()` (the Phase 5 supervisor graph).
- **Backends sit behind interfaces** (`backends/`). `Embedder`, `Reranker`, `DocumentParser` and `SearchProvider` are `typing.Protocol`s, each with one real implementation and a fake. Chat models use LangChain's `BaseChatModel` as their interface, with a replay model as the fake. Swapping the embedder (PROPOSAL §7.1) is then a configuration change.
- **The interfaces are multimodal by contract.** `DocumentParser` returns typed elements with their page, bounding box and crop reference. An `Embedder` input is text, a crop, or both together. A `Reranker` candidate is text or a crop, and candidates are scored one at a time, because a batch mixing images and text crashed the reranker in Day 2. The fakes accept the same inputs, so a graph test exercises the table, chart and picture paths without a GPU.

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
│   └── processed/         # per document: Docling Markdown/JSON artifacts and crops named by hash (§3)
├── docs/
│   └── DESIGN.md          # this document
├── migrations/            # Alembic migrations for PostgreSQL + pgvector
├── services/
│   └── models/            # small internal HTTP server on the GPU: /parse, /ocr, /rerank, /sentiment
├── skills/                # skill folders: SKILL.md + scripts + references
├── src/
│   └── finrag_engine/     # Python package: tools, agents, pipelines, worker
├── tests/                 # unit tests per tool module; each skill script run on a fixture document
└── notebooks/             # 01–06, the deliverable demos (PROPOSAL §5); scratch/ is
                           # git-ignored throwaway exploration
```

<details>
<summary><b><code>src/finrag_engine/</code> in detail</b></summary>

```
src/finrag_engine/
├── engine.py              # Engine: composition root and public API (ingest, refresh, retrieve, answer, ask)
├── context.py             # EngineContext: frozen dataclass of settings, stores, backends and the tracer
├── tracing.py             # Tracer: the one emitter for the run record (PostgreSQL) and Langfuse; masking (§12)
├── run.py                 # RunResult, StopReason, Budget (§5)
├── errors.py              # ToolDenied, ToolError and its failure classes (§5)
├── config.py              # Settings: .env + settings.yaml + sources.yaml (§10)
├── worker.py              # arq worker: scheduled refresh + queued document refreshes, through Engine
├── backends/              # swappable services behind interfaces
│   ├── protocols.py       # Embedder, Reranker, DocumentParser, SearchProvider
│   ├── models_service.py  # HTTP client for the models service (/parse, /ocr, /rerank, /sentiment)
│   ├── openrouter.py      # chat models (primary, fallback, vision, guard) and the embedder
│   ├── exa.py             # web search
│   └── fakes.py           # in-memory fakes and a replay chat model, for tests
├── schemas/               # Pydantic contracts shared across modules (arguments models live with their tool)
│   ├── chunks.py          # Chunk envelope (§7), ParsedDocument, Element, Embedding
│   ├── multimodal.py      # ChartRouting, ChartSeries, ChartCaption, TableVerification, ChartVerification
│   ├── records.py         # Instrument, Document, FinancialFact, Price, NewsItem, AnalystRating,
│   │                      # RecordBatch, ChunkRef, RecordRef
│   ├── acquisition.py     # SearchResult, AcquiredItem, IngestReport, DedupeResult, NewsClassification
│   ├── retrieval.py       # Predicates, Hit, EvidenceSet, SufficiencyResult
│   ├── news.py            # NewsDigest, Consensus, NewsTrend, ClaimCheck
│   ├── analysis.py        # MetricValue, Comparison, PriceTrend
│   ├── answer.py          # Claim, Draft, VerificationResult, SafetyVerdict, Answer
│   └── agents.py          # Plan, the four briefs, AcquisitionReport, FreshnessReport, Skill (§5)
├── stores/                # PostgreSQL access (SQLAlchemy + pgvector). ORM models are private here;
│                          # store classes take and return schemas (§7). Corpus stores and the
│                          # run-record store are separate classes (§12)
├── prompts/               # prompt templates for model-calling tools outside ingestion (route, draft_claims)
│                          # and the sub-agents' system prompts
├── tools/                 # every tool in §5: typed input/output, no inline prompt
│   ├── registry.py        # ToolSpec per tool: arguments model, return model, read_only flag
│   ├── gateway.py         # call_tool: the only path from an agent or a skill script to a tool
│   ├── acquisition.py     # search_web, fetch_document, scrape_page, fetch_news,
│   │                      # tipranks_mcp, scrape_authenticated, ingest
│   ├── parsing.py         # parse_document (Docling), route_ocr, build_chunks
│   ├── multimodal.py      # classify_chart, extract_chart_series, caption_chart
│   ├── verification.py    # verify_table, verify_chart
│   ├── extraction.py      # extract_records, resolve_entity, dedupe
│   ├── embedding.py       # embed
│   ├── retrieval.py       # retrieve, search, record_lookup, rerank, check_sufficiency
│   ├── news.py            # classify_news, news_window, analyst_consensus, news_trend, verify_claim
│   ├── analysis.py        # extract_metric, compare_periods, compare_companies, price_trend, draft_claims
│   ├── harness.py         # route, check_freshness
│   └── guardrails.py      # verify_claims, check_safety
├── agents/                # LangGraph agents: a prompt + an allowlist of tools and skills
│   ├── registry.py        # one AgentSpec per sub-agent (§5)
│   ├── factory.py         # make_agent_node(spec, ctx): builds every sub-agent from its spec
│   ├── state.py           # supervisor state (TypedDict) holding the Plan and the artifacts (§5)
│   ├── skill_loader.py    # parses every SKILL.md into a Skill; load_skill, read_skill_reference, run_skill_script
│   └── supervisor.py      # supervisor graph: input gate → route → check_freshness → plan walker
│                          # → output gate (pipelines/answer.py + check_safety)
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

A `SKILL.md` starts with frontmatter whose name and description are always visible to the agent; the six sections are loaded only on demand:

```markdown
---
name: source-selection
description: Choose which sources to acquire for an instrument and which web searches to run, by instrument type and staleness. Use before any acquisition.
version: 1
owner: data-aggregator
---
## Assumptions
- The instrument is in ListEquities.csv and its sources are in config/sources.yaml.

## Constraints
- Permitted sources only; company-issued documents come first.

## Tools
- search_web
- fetch_document
- scrape_page
- fetch_news

## Procedure
- Start from the instrument's configured sources.
- Read references/search_guide.md before any web search ...

## Verification
- Every fetched item has a source URL and a publication date.

## Failure modes
- no permitted source has the document -> stop; report it under `failed` in the AcquisitionReport
```

</details>

## 9. Runtime and infrastructure

**One package, several entry points.** `finrag_engine`, the AI engine, holds all the logic as tools and LangGraph graphs. The notebooks, the ingestion worker and the tests all call it through one `Engine` object (§8). Each phase is demoed in a thin notebook, in the course's style, but nothing a notebook proves is re-implemented elsewhere.

**Productionization.** Moving the engine into an agent harness compatible with LangGraph is outside the capstone's scope, and its design is still to be defined. This section covers what the capstone runs.

### 9.1 Services

```mermaid
flowchart TB
    NB["Notebooks and tests<br/>WSL2 uv environment, finrag_engine graphs"]
    RQ[("redis<br/>job queue")]
    W["worker<br/>arq: scheduled + queued document ingestion"]
    M["models (NVIDIA GPU)<br/>Docling /parse, LightOnOCR /ocr,<br/>Qwen3-VL-Reranker /rerank,<br/>sentiment classifier /sentiment"]
    PG[("postgres<br/>pgvector chunks + typed records<br/>+ run and ingestion records")]
    OR["OpenRouter<br/>agent LLM, vision model, embeddings"]
    EXT["Sources<br/>SEC, IR, SPDR, Stooq, Exa, TipRanks MCP"]
    LF["Langfuse Cloud<br/>masked traces + scores"]

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

`docker compose up -d` runs the four services below. Notebooks and tests run outside Docker, in the WSL2 `uv` environment, and reach the services on `localhost`. The repository's `data/` folder is bind-mounted into `worker` and `models`, so a crop the parser writes is the same file a notebook opens. If `models` moves to a remote GPU, `/parse` returns the artifacts in its response and the client writes them to the same layout.

<details>
<summary><b>Compose services</b> (4)</summary>

| Service | Image | Role |
|---|---|---|
| `worker` | `docker/app.Dockerfile`: Python + `uv`, the package's locked dependencies (no `dev` or `models` group) and `src/` (no `skills/`: no agent runs in the worker), non-root user; command `arq` | Scheduled refresh per `refresh.*`, acquiring from `config/sources.yaml` with no agent. Also runs the filing and fund-document refreshes queued when a question finds them stale; prices, news and ratings refresh inline in the supervisor graph |
| `models` | `docker/models.Dockerfile` (CUDA, torch, Docling, transformers) | The only GPU user: `/parse` (Docling with its picture classifier), `/ocr` (LightOnOCR-2-1B), `/rerank` (Qwen3-VL-Reranker-2B), `/sentiment` (news sentiment classifier). A small internal HTTP server that serves models, not users. Loads each model on first use. On the RTX 3080 Ti (16 GB) all four stay loaded (~8–10 GB; the sentiment classifier adds little); `models_service.idle_unload_seconds` enables the Day 2 unload-when-idle pattern on smaller GPUs |
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
- **Tests:** pytest markers `integration` (needs the compose services), `gpu` (needs the `models` service) and `network` (external sites and APIs). Plain `pytest` runs only the unmarked unit tests; `pytest -m integration` and the others opt in. Graph tests are unmarked: they run on an `EngineContext` built from the fakes and a replay chat model (§8), so they need no key and no service.
- **Notebooks in git:** outputs are stripped on commit (`nbstripout` as a git filter), so diffs show code only. Seeing results means running the notebook.
- **Tracing:** from Phase 1, every graph run, in a notebook or the worker, stores its record in PostgreSQL and sends a masked trace to Langfuse Cloud, with keys from `.env` (§12).

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
| `config/sources.yaml` | The source catalog (§2): URL templates per instrument type, per-instrument overrides and each source's truth coefficient (§4) | Yes |

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
POSTGRES_DB=finrag
# URLs as seen from WSL (notebooks, tests); compose.yaml gives the worker service-name URLs
DATABASE_URL=postgresql://user:password@localhost:5432/finrag
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
  recency_half_life_days: 7         # source weights come from source_truth in config/sources.yaml (§4)

parsing:
  ocr_min_chars: 400                # per page, counted on the PDF's text layer (§3)
  ocr_min_image_fraction: 0.5       # and a raster image must cover this share of the page; then LightOnOCR recovers it
  images_scale: 2.0                 # Docling crop resolution

chunking:
  max_chars: 2000                   # a text chunk is a heading plus its body up to this size; it never crosses a heading

charts:
  max_enriched_per_doc: 20          # pictures per document sent to the VLM; the rest stay docling_picture chunks (§3)
  skip_classes: [logo, icon]        # Docling picture classes dropped before any vision call (§3)
  min_short_side: 100               # pixels at images_scale; smaller pictures are dropped as decorative

tables:
  max_verified_per_doc: 30          # vision re-reads per document, after the XBRL check; the rest stay not_attempted (§3)
  max_prose_cell_chars: 200         # a "table" whose median cell is longer, with no numeric column, is demoted to text

drafting:
  include_crops: [chart, picture]   # chunk kinds whose crop is sent to the drafting step; [] sends text only (§4)
  crop_max_side: 1400               # pixels; crops are downscaled before they are sent to a model

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
  source_truth_weight: 1.0          # exponent on (source_truth / 10) applied to the rerank score; 0 switches it off (§4)

records:
  agreement_tolerance: 0.01         # relative difference above which two sources' values for one metric and period disagree (§4)

models_service:
  idle_unload_seconds: 0            # 0 = keep models loaded. RTX 3080 Ti laptop (16 GB) holds all four models
                                    # together (~8-10 GB); set e.g. 600 on a smaller GPU
  dtype: bfloat16                   # native on Ampere (RTX 30xx)

policy:
  enforce_provenance: true          # magnitude claims must cite a chunk with a second source
  require_agreement: false          # strict level: only agreed tables or typed records (§4)
  min_source_truth: 0               # 0 = every source admissible; e.g. 8 excludes analyst reports and news when a figure is needed (§4)
  max_draft_retries: 1              # redrafts after a failed provenance check before the run stops (§4)
  max_supervisor_steps: 25          # LangGraph recursion limit for the supervisor graph (§4)
  max_agent_steps: 8                # each sub-agent's own loop (§4)
  max_tool_calls: 30                # per run, counted by the tool gateway (§5)
  max_model_calls: 20               # per run, counted by the chat backend (§5)

scraping:
  requests_per_minute: 20
  tipranks_scrape_fallback: false   # stays off until TipRanks' terms are checked

openrouter:
  data_collection: deny             # every chat and embedding call routes only to providers that don't retain payloads

models:
  primary: <openrouter model id>         # must accept images while drafting.include_crops is not empty (§4)
  fallback: <openrouter model id>        # same requirement
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
  max_usd_per_query: 0.50           # part of the per-run Budget (§5)

tracing:                            # §12
  langfuse_enabled: true            # false in unit tests; the run record in PostgreSQL is written either way
  ingestion_detail: step            # step = one observation per sub-graph step per document;
                                    # element = one per chunk or chart, for debugging a single document
  mask_sources: [tipranks]          # payloads from these sources leave as ids and counts, never as content

logging:
  level: INFO                       # every record carries run_id and trace_id (§12)

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

source_truth:                # 1-10: how far a figure from this source can be trusted as a fact of record (§4).
                             # Every source must have one; a source without a coefficient fails at startup
  sec_xbrl: 10
  sec_filing: 10
  company_ir: 10             # annual reports, decks and press releases issued by the company
  fund_documents: 10         # the issuer's fact sheets, prospectuses and annual reports
  transcript: 8
  fundamentals: 8            # third-party data (Stockanalysis)
  prices: 8                  # third-party data (Stooq)
  analyst_ratings: 7         # TipRanks ratings
  analyst_report: 6          # third-party, AI-written reports such as TipRanks' AI analyst report
  news: 5                    # third-party articles and web pages
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
  - Provenance test: a forged numeric claim citing a chart must be blocked (after at most one redraft), and must pass only when `enforce_provenance` is switched off. It is run with `drafting.include_crops` on and off, since seeing the crop is what tempts a model to read a number from it.
  - Table checks: the share of tables settled by XBRL alone, by a vision re-read, and left `not_attempted` at the `tables.max_verified_per_doc` cap.
  - One mixed document (text, tables, charts, pictures, a scanned page) run end to end on the fakes: every element kind reaches the store with its crop reference, and no state or record holds image bytes.
- **Layout regression (Phase 1+)**, the cases the October 2026 validation run exposed (§3). The validation report is subscriber content and can't be committed, so the fixture is a synthetic PDF that reproduces its layouts:
  - A logo on every page and a badge beside a heading cost no vision call, and every real chart is still enriched.
  - A card under a column heading keeps that heading in its `section` path, on a page where a full-width block sits between the two columns.
  - A card of prose that Docling returns as a table is stored as text.
  - A header-less grid of label-value pairs is stored as pairs, and a dash in it is a missing value.
  - A chart whose legend sits outside its box is sent to the vision model with that legend.
  - A value printed in a chart is quotable; a value read from the same chart's bars is blocked.
  - A born-digital page holding only a chart and a table is not sent to OCR.
  - A table with group rows, a wrapped row label and mixed unit suffixes yields the right label, value and unit for every cell.
- **Source truth (Phase 1+)**, the rules of §4:
  - For the same fact, a filing passage outranks a third-party report passage of similar relevance. With `retrieval.source_truth_weight: 0` the order is by relevance alone.
  - When a report's table and XBRL give different values for one metric and period, `record_lookup` returns the XBRL record and the answer shows the disagreement.
  - With `policy.min_source_truth` above a source's coefficient, its chunks are not retrieved for a question that needs a figure, and a magnitude claim citing it is blocked.
  - Every citation in an answer shows its coefficient, and a stored run shows the coefficient that was in force.
- **Comparison correctness (Phase 2+):**
  - `compare_periods` / `compare_companies` / `price_trend` unit-tested against hand-computed results.
  - Spot checks of fiscal-period alignment across companies.
- **Trajectories:** Phase 4 ranks the answer graph's paths (sufficiency stop, redraft). Phase 5 applies the same harness to the agents: right plan (e.g. Market Intelligence, not the Researcher, for a news question), right tool within a sub-agent, the skill loaded when the task needs it, and self-correction when wrong.
- **Traceability and observability (Phase 1+)**, the rules of §12:
  - Every run, including a stopped one, has a `runs` row, and every source a claim cites has a `run_evidence` row whose hash matched the chunk or record when it was read.
  - Replaying a stored run after its document is re-ingested reports which sources changed.
  - A run with Langfuse unreachable still completes and stores its record.
  - A planted key and a TipRanks payload never appear in an exported span.
  - Ingesting one document at `ingestion_detail: step` stays under a fixed number of observations.
  - From Phase 4, every evaluation score is a `run_scores` row and the same score in Langfuse.
- **Gates and screening (Phase 5):**
  - An injection in the question stops at the input gate.
  - An instruction planted in a scraped page, news item or MCP result is flagged at ingestion and never retrieved.
  - An unsafe draft stops without a redraft.
  - A draft that fails provenance twice stops with its reason.
  - `check_safety`'s rules are unit-tested on known injection patterns, without the guard model.

## 12. Traceability and observability

The capstone's claim is that every figure is traceable to its source. That has to hold after a trace has expired and after a document has been refreshed, so two records are kept apart:

| | Traceability | Observability |
|---|---|---|
| Question it answers | Which source did this figure come from, and can it be shown again? | What did this run do, and what did it cost? |
| Where it lives | PostgreSQL, in the record tables (§7) | Langfuse Cloud |
| Lifetime | Permanent | The plan's limits: 30 days and 50k units a month on the free plan (October 2026) |
| Role | The record | A viewer and an evaluation UI |

**Nothing is recorded only in Langfuse.** Langfuse is used because the course's Day 3 notebooks are written against it, it masks data on the client, and it can be self-hosted later by changing `LANGFUSE_HOST`.

### 12.1 The run record

Every `engine.answer()` and `engine.ask()` call writes a run record, including a run that stopped. An answer is returned only after its record is stored.

- **`runs`** has one row per run: the question, the `Plan`, the answer, the `StopReason`, the policy level and the versions that produced it (git commit, settings hash, prompt, model and skill versions). Versions are captured at run time because they can't be recovered later.
- **`run_evidence`** is the evidence locker of the Day 1 hands-on: every chunk and record handed to the drafting step, stored as it was read, with its hash, the source-truth coefficient in force and whether a claim cited it. For a table, chart or picture it also stores the crop's path and hash, and the crop file itself is never overwritten (§3). A replay compares each hash with the live chunk or record and reports the sources that changed, so an old answer stays provable after a refresh.
- **`run_steps`** is the trajectory: one row per plan step, model call, tool call and gate, in order, with its input and output, outcome, tokens, cost and latency. Sub-agent histories are kept here, under their plan step.
- **`run_scores`** holds every score given to a run: gate results, hard checks, Ragas and judge scores. Rows are only added, so a run can be scored again by a new scorer.
- **`scenario_id`** on `runs` groups runs of the same question: the QA-set id for an evaluation run, otherwise a hash of the normalised question.

**Ready for a self-improving agent.** `run_steps` keeps each agent's history in chat-message form with its tool calls and results, `scenario_id` groups comparable runs, and `run_scores` holds rewards. That is what trajectory ranking (RULER) and a learned router (AgensFlow) consume, so exporting to them is a mapping, not a re-run. A learned router would replace `route`'s planning call; the plan walker and the gates stay code. No learning code is in scope.

### 12.2 The ingestion record

Every ingested item writes an `ingest_runs` row: the parser, OCR, vision and embedding models, the prompt versions, the settings hash, the per-document counts (pages, OCR pages, chunks by kind, charts enriched and skipped, tables verified, not attempted and demoted to text, pictures dropped as decorative or uncaptioned, unmapped characters, records written) and the model calls, tokens and cost it used. Every rule in §3 that drops or changes something has a count here, so nothing disappears silently. Chunks and typed records point to it through `ingest_run_id` (§7). A chunk can therefore be traced to the exact models that produced it, which matters when the embedder is swapped (PROPOSAL §7.1).

### 12.3 Tracing

- **One emitter.** `Tracer` (`tracing.py`) is the only code that writes the run record or talks to Langfuse. It is called at the engine's boundaries: `Engine` (start and end of a run), the plan walker, the tool gateway, the chat backend and the steps of the sub-graphs. Tools and graph nodes never trace themselves. On the Langfuse side it attaches Langfuse's LangChain callback handler to each answer and agent run. For ingestion at `step` detail it emits one observation per step itself, so the vision calls made for each chart and table don't each become one (§12.4).
- **One trace per `Engine` call**, named after the call (`ingest`, `refresh`, `retrieve`, `answer`, `ask`). Its id is stored on the `runs` or `ingest_runs` row, and each `run_steps` row stores its observation id, so the two records can be joined.
- **Trace attributes.** Session: the notebook session, worker job or evaluation batch. Tags: the entry point, the tickers and the environment (notebook, worker, test, evaluation). Metadata: run id, scenario id, policy level, settings hash and git commit.
- **Scores.** Each `run_scores` row is also sent to Langfuse as a score on the trace. PostgreSQL is authoritative if the two differ.

### 12.4 Rules

- **Mask at the emitter.** A mask function runs on the client before a span is exported. It removes anything that looks like a secret, and replaces any payload or chunk whose document source is listed in `tracing.mask_sources` with its ids and counts, however it was acquired (MCP, authenticated scrape or a downloaded report). Secrets are masked in both records. Subscriber content is masked only on export, because PostgreSQL already holds it as typed records.
- **Images travel as references.** The run record and the traces carry a crop's path and hash, never its bytes. The mask function replaces an inline image in a model call with that reference before export, so a vision call costs one observation and a few hundred bytes, not a base64 payload.
- **Langfuse never fails a run.** If it is unreachable or the unit cap is reached, the run continues, its record is stored and the gap is logged.
- **Stay inside the unit budget.** A Langfuse unit is a trace, an observation or a score. Ingestion emits one observation per sub-graph step per document (`tracing.ingestion_detail: step`); per-element detail goes into the `ingest_runs` counts. Agent runs trace every model call and tool call.
- **Logs are for operating, not for tracing.** The package uses the standard `logging` module, one logger per module. Every record carries `run_id` and `trace_id`, and no record at `INFO` contains document text or a credential. Logs explain what the worker and the `models` service are doing; the run record explains a run.
