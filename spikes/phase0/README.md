# Phase 0 checks

Each script here proves or disproves one assumption the design rests on, before any engine code depends on it. This file holds the rules of a script and the results. The loop a check follows is in [DESIGN §9.2](../../docs/DESIGN.md#92-development), and the order of the checks is the queue of each phase's slice file ([docs/slices/](../../docs/slices/)); neither is repeated here.

- **Isolated.** A script declares its own dependencies in its header and runs with `uv run <script>` inside a disposable container (`docker run --rm`). The container sees this folder, read-only, and nothing else of the project. A service a check needs, such as PostgreSQL, runs in a second container on a private network. Nothing is installed into the project or the system; `uv cache clean` removes what was downloaded. Checks 1 to 6 ran before this rule, directly in WSL.
- **Criterion first.** The pass criterion is written in the script's docstring before the script is run, and the script prints `RESULT: PASS` or `RESULT: FAIL` against it, with the library versions it used.
- **A fallback for each.** A failure changes a line in the design; it does not stall the build.
- **No secrets printed, no metered calls.** The container does not see `.env`: the command that starts it reads the keys a check needs and passes them to that run only.
- **A dated record, not a test.** A result holds for the versions and the date shown. Re-run a script when its dependency is upgraded. A script's file number is its check number. One row per assumption: when a script tests several, each takes a letter (13a, 13b). A new script pins a library an earlier check recorded to that version.

## Results

| # | Assumption | Needs | Result | Evidence | If it fails |
|---|---|---|---|---|---|
| 1 | LangGraph runs a sync node in a thread when the graph is run async, and each run sees its own run context | Nothing | **Pass** (2026-10-03) | langgraph 1.2.12. Two runs with a 1 s blocking sync node finished in 1.04 s and the event loop kept ticking | Make every node async |
| 2 | A scripted fake chat model can return tool calls and structured output, and goes through the budget and fallback wrapper | Nothing | **Pass** (2026-10-03) | langchain-core 1.6.6. The stock fake raises `NotImplementedError` for both; a 25-line fake of our own passes, sync and async | Write a larger fake |
| 3 | Which host variable the Langfuse SDK reads | Nothing | **Pass, with a change** (2026-10-03) | langfuse 4.16.0 reads both. `LANGFUSE_BASE_URL` wins when both are set, and `LANGFUSE_HOST` is marked deprecated | Rename one variable |
| 4 | A parse answer carrying every crop of a long filing stays a workable size | Docling on CPU | **Pass, with a change** (2026-10-03) | docling 2.133.0 on Apple's 80-page FY2025 10-K: 7.7 MB in total (52 table and 2 picture crops 4.9 MB, JSON 2.5 MB, Markdown 0.35 MB); 102 s on CPU | Compress the answer, or keep a shared folder |
| 5 | OpenRouter embeds an image with Gemini Embedding 2, in the same space as text | OpenRouter key | **Pass** (2026-10-04) | `google/gemini-embedding-2` returns normalised vectors of 1536 values for a text, a PNG, and a crop with its text together, with `data_collection: deny`. A question ranks the right page image first (0.49 against 0.34). 258 tokens an image; $0.0004 for six items | The local embedder named in PROPOSAL §8.1 |
| 6 | A DeepEval judge on OpenRouter separates a correct answer from one with a wrong figure, by more than its own run-to-run noise; it scores a case whose context is an image; what a run costs | OpenRouter key | **Pass** (2026-10-04) | deepeval 4.2.8, judge `google/gemini-2.5-flash-lite`. The correct answer scored 1.0 three times on faithfulness and on G-Eval; the wrong figure scored 0.0 on both. The image case scored 1.0 against 0.0. 24 s, $0.0006 | Judge text only; chart cases rely on hard checks |
| 7a | Docling and LightOnOCR fit in 16 GB inside a GPU container | Docker and the GPU | **Pass** (2026-10-04) | See "Check 7a" below | `models_service.idle_unload_seconds` |
| 7b | The reranker and the sentiment classifier fit beside them, all four models loaded | The `models` image of slice 0.6 | Not run | | `models_service.idle_unload_seconds` |
| 8 | The Stooq, Stockanalysis and SPDR downloads work within their terms | Network | **Fail for Stooq and Stockanalysis; for SPDR the fact sheets pass, the prospectus and annual report are not reachable at the issuer** (2026-10-12). The SEC route for those two is check 15 | httpx 0.28.1, beautifulsoup4 4.12.3; 36 of 80 requests. **Stooq, fail.** Its `robots.txt` has `User-agent: *` with `Disallow: /`. Both CSV URLs answered an HTML page ("This site requires JavaScript to verify your browser"), not CSV. No terms link was found on the home page, so its terms are unread. **Stockanalysis, fail.** `robots.txt` allows the page, but its seven Download elements are buttons with no file address. Its terms say: "You may not use bots, scripts, scrapers, browser automations or any other automated or programmatic methods to access the platform, collect data from it, or download data in bulk." **SPDR, partial.** The fact sheets of all 11 funds are real PDFs that `robots.txt` allows. No prospectus or annual report appears in the static HTML of any fund page (`sectorspdrs.com/mainfund/{ticker}`). The XLK address `/api/documents/by-fullname/...` returned 404. No terms link was found. Whether the 11 funds share their prospectus and annual report is therefore open. **Script faults:** it fetched the two Stooq URLs before acting on the `robots.txt` answer, and one terms link led to a news article. **SPDR, second pass** (`spdr2`; pypdf 5.1.0; 22 of 50 requests). `sectorspdrs.com/mainfund/{ticker}` redirects to `www.ssga.com/mainfund/{ticker}`. The fact sheet template `https://www.ssga.com/library-content/products/factsheets/etfs/us/factsheet-us-en-{ticker}.pdf` returned a real PDF (about 230 KB) for all 11 funds, and `robots.txt` allows it. The prospectus, annual report and SAI links on the fund page are hash links into a JavaScript viewer. The `sectorspdrs.com/api/documents/by-name/{name}` address answered HTML for "Prospectus" and 404 for the three annual-report names and for the control (the SAI name), every answer redirected to `ssga.com/fr/en_gb/...`; whether that is geographic was not tested. Whether the 11 funds share a prospectus and an annual report is therefore still open. The script's terms discovery picked two unrelated articles again. The terms page, read by hand with a summarising fetch tool (`ssga.com/us/en/intermediary/footer/terms-and-conditions`), has no sentence on robots, scraping or automated access; it limits copying to "print-outs for your own personal use" or "a reasonable number of copies … for internal use within your organization", says services are "for your business purposes only", and bars redistribution. Quotes are the tool's extraction and are to be checked at the page; the verdict on terms is the user's | Alternative sources |
| 9 | Docling's layout model finds the picture boxes on an image-only page, so they can be masked before OCR | The GPU | Not run | | OCR each layout region separately |
| 10 | pgvector queries with SQL predicates, and a full-text candidate list under the same predicates, work through psycopg 3 from async SQLAlchemy | The `postgres` container | Not run | | Sync driver wrapped in a thread, behind the same async store methods |
| 11 | A masked trace and its scores reach Langfuse, tagged with the slice and the git commit | Langfuse keys | Not run | | The run record in PostgreSQL alone; Langfuse stays optional |
| 12 | Ragas' `TestsetGenerator` produces usable questions from our documents through OpenRouter | OpenRouter key | Not run | | DeepEval's `Synthesizer` |
| 13a | LangGraph's Postgres checkpointer works beside our own stores: on psycopg 3, in its own schema, untouched by Alembic | The `postgres` container | **Pass** (2026-10-12) | langgraph-checkpoint-postgres 3.1.2, psycopg 3.3.6, sqlalchemy 2.1.4, alembic 1.20.0. `setup()` made its 4 tables in the schema `memory` and none in `public`. Our own table works through async SQLAlchemy beside them. An Alembic create and drop left the row counts unchanged (10 checkpoints, 22 writes). Autogenerate proposes touching `memory` unless a schema filter skips it | The run record's `run_steps` as the resume log |
| 13b | A graph with a sub-graph and a run context resumes after its process is killed, without repeating a completed step | The `postgres` container | **Pass** (2026-10-12), after a failed first run the same day | langgraph 1.2.12, langgraph-checkpoint 4.2.0. Three SIGKILL kills resumed: between parent steps, inside a sub-graph, and in the second visit to the same node. No completed step ran again, the cut-short step started twice and ended once, and the final spend and trail were exact. Saves used `durability="sync"`; the default was not tested. The first run failed (spend 6 instead of 5) because my test sub-graph shared the parent's state keys. That was a test fault and no step had repeated | The run record's `run_steps` as the resume log |
| 13c | LangGraph's Postgres store works in the same schema, and its semantic search runs at our embedding dimensions | The `postgres` container | Not run | | Tables of our own for preferences and run records |
| 13d | A node timeout exists on our LangGraph version | Nothing | **Pass** (2026-10-12) | `add_node(timeout=0.3)` exists and raised `NodeTimeoutError` after 0.30 s; tried with a number only, not a `TimeoutPolicy`. `step_timeout` is a step-level setting. `asyncio.timeout` in a wrapper also stopped the node (0.31 s) | `asyncio.timeout` in a node wrapper |
| 13e | A resume works across two levels of nesting: a node calls a sub-graph whose node calls an inner graph, as `retrieve` would | The `postgres` container | **Pass** (2026-10-12) | Kills between the levels and inside the inner graph both resumed. Completed work was not rerun at any level, `tool` and `i2` were cut short once each, and each level counted its spend once (6 in total) | Run the retrieval graph in a tool without checkpoints of its own, so a cut-short `retrieve` reruns whole |
| 14 | The official MCP Python SDK can carry the TipRanks client: a tool call is never sent twice after a timeout or a dropped connection; a quota refusal reaches the caller as an error it can tell from any other; the handshake and the catalogue cost no metered call | A local fake MCP server for the first two; the network and the free usage counter for the third. No metered call | Not run | | Our own JSON-RPC calls over an HTTP client, as the 2026-10-03 check made with `curl` |
| 15 | The SEC filings of the Select Sector SPDR Trust (CIK 1064641) give the prospectus and the annual report of the 11 funds by plain download, and show whether the funds share them | The network; a User-Agent with a contact address that the user chooses (SEC fair access) | Not run | | The fact sheet only for fund documents; the prospectus and annual report come by the supplied-file path (DESIGN §2) |
| 16 | Tiingo and Twelve Data, on their free plans, deliver daily prices for the 19 instruments (11 ETFs, 8 equities) within their limits, and their terms allow keeping the data in the local database | A free key for each provider in `.env`; the network | **Technical pass for both** (2026-10-13). Terms: Tiingo's free plan bars persistent storage, as read, so Twelve Data is the primary and Tiingo the fallback (the user's decision, 2026-10-13) | httpx 0.28.1. **Tiingo:** 28 requests of the 50 an hour; all 19 instruments returned 251 rows for the 365 days to 2026-10-12, with no high/low/volume inconsistency and no duplicate date; one ticker per call; no rate-limit headers and no usage counter; the supported-tickers file lists all 19 (start dates from 1980; XLRE 2015, XLC 2018, IONQ 2021). **Twelve Data:** about 31 credits counted of the 800 a day (the usage counter moved from 1 to 28); the same 251 rows for all 19 in three batched calls; credits are 1 a symbol, so batching saves requests and not credits; the OpenAPI file lists 187 endpoints with plan and credit cost, and `/analyst_ratings/light`, marked Ultra, answered on the free key. **Agreement:** raw closes of the two providers agree on all common days for all 19 (largest difference 0.0013%); adjusted closes agree within 0.0064% for XLK and AAPL. **Terms, read through a summarising tool and to be checked at the pages:** Tiingo (`app.tiingo.com/tos`, updated 2026-10-06) §1.6(a) bars persisting data on a Starter or trial plan, allows it on paid plans "to the extent permitted", and restricts derived products; Twelve Data (`twelvedata.com/terms`, updated 2026-01-01) §2.2(a) allows storing data "solely for Internal Use" and §2.3(g) bars storing it beyond the timeframes in its documentation, which were not found. **Fundamentals on paid plans (read 2026-10-13, from the pricing page and the OpenAPI file):** **Twelve Data** includes them by plan, at a credit cost per symbol: Grow ($29 a month and up) has `/profile` (10), `/dividends`, `/splits`, `/earnings` (20 each) and the calendars; Pro ($99 and up) adds `/income_statement`, `/balance_sheet`, `/cash_flow` (100 each) and `/statistics` (50); Ultra ($329 and up) adds the consolidated statements, `/market_cap` and `/key_executives`; the free Basic plan has none of these (only `/press_releases`). **Tiingo** sells fundamentals as a separate add-on, "not included in the Starter or Power plan prices", with individual and commercial tiers priced on request; it covers 29,980 stocks and 45,161 funds and ETFs, and the free key reaches only the list of 85 data definitions. The studies are `Resources/tiingo_capabilities.md`, `Resources/twelvedata_capabilities.md` and `Resources/twelvedata_openapi_catalogue.json` | The other provider; a Tiingo paid plan if its storage is to be used |

## Check 7a: Docling and LightOnOCR on the laptop GPU

`07a_gpu_docling_lightonocr.py` on the RTX 3080 Ti Laptop GPU (16 GB), with Apple's 80-page FY2025 10-K (torch 2.14.1, docling 2.133.0, transformers 5.18.0). It ran twice: on 2026-10-03 in a disposable `uv` environment in WSL, and on 2026-10-04 inside a container with `--gpus all`, on mains power, with fonts installed.

| Measure | In WSL (2026-10-03) | In a container (2026-10-04) |
|---|---|---|
| Docling, 80 pages, including model load | 28 s, 0.35 s a page (1.28 s a page on CPU) | 30 s, 0.38 s a page |
| Tables and pictures Docling found | 52 and 2 | 52 and 2 |
| Docling's GPU memory | 1.4 GB | 1.4 GB |
| LightOnOCR, one rasterised page at a time | About 20 s a page, 38 tokens a second | 20.4 s a page, 38 to 41 tokens a second |
| LightOnOCR, four pages in one batch | 24.5 s in one run and 109 s in another | 108 s |
| OCR accuracy against the PDF's text layer | 100% of words and numbers on three pages; 91% of words on the page with a chart | The same |
| GPU memory with both models loaded | 2.5 GB held, 5.3 GB at peak | 2.5 GB held, 5.3 GB at peak |
| GPU load during the run | 43% on average, above 95% for 4% of the time; 74 °C and 125 W at most | 48% on average, above 95% for 6% of the time; 78 °C and 125 W at most |

The laptop is not saturated by these two models, and the container costs neither speed nor memory. OCR speed is limited by the token-by-token generation loop, not by the GPU. Batching is not dependably faster: four pages in one batch took longer than four single pages in two runs out of three.

## What the checks changed

- **Check 1** also measured the opposite case: an async node that calls blocking code directly. Two runs then took 2.03 s and the event loop ticked once. Blocking code inside an async function stops every run in the process.
- **Check 3**: the design's `LANGFUSE_HOST` works today but is deprecated. Use `LANGFUSE_BASE_URL`.
- **Check 4**: Docling's JSON embeds every page image, even when asked for placeholders: 42.9 MB for 80 pages, against 2.5 MB once they are removed. The `models` service must drop the page and picture images from the JSON it returns; the crops travel as files.
- **Check 5, text ranks above images**: a question is closer to a sentence that describes the chart (0.67) than to the chart's image (0.49). Sending the caption with the crop raises the image to 0.57. It confirms the need for the design's modality normalisation, where text chunks and crops are scored within their own kind before they are merged (DESIGN §4; slice 1.16).
- **Check 6, what it does not show**: the wrong figure was blatant and every score landed on 0 or 1, so the check says the judge works, not how noisy it is on a borderline answer. That is measured when the baseline is recorded (`evaluation.confirm_repeats`). DeepEval's HTTP client also logs "Event loop is closed" when it cleans up; it is harmless in a script and worth watching once the judge is called from the async engine.
- **Check 7a, fonts**: in an image with no fonts, Docling draws every glyph as an empty box and reports nothing. Its layout model then sees boxes instead of text: on the 10-K it found 45 tables instead of 52, 94 headings instead of 225 and 1 picture instead of 2. With `fontconfig` and `fonts-dejavu-core` installed, the Markdown and the page images are byte-identical to the WSL run. The `models` image must install fonts, and the font set is part of what makes ingestion repeatable (DESIGN §9.1).
- **Check 7a, system libraries**: Docling pulls in `opencv-python`, which fails to import in a slim image (`libxcb.so.1`). The image needs `libgl1`, `libglib2.0-0`, `libsm6` and `libxext6`.
- **Check 7a, the compiler**: PyTorch 2.14 routes some GPU operations through Triton kernels, which need a C compiler and Python headers the first time they run. WSL has neither, and LightOnOCR failed until `TORCH_DISABLE_NATIVE_JIT=1` was set. The `models` image must either include a compiler or set that variable.
- **Check 7a, Docker**: Docker here is Docker Engine installed in the WSL distribution. `--gpus all` failed until the NVIDIA Container Toolkit (1.20.1) was installed on 2026-10-04. Docker Desktop is installed too but is not used.
- **Check 7a, power**: on battery the laptop caps the GPU near 48 W, and OCR took 36 s a page instead of 20. Time GPU work on mains power only.
- **Check 7a, charts on an OCR page**: LightOnOCR describes a chart in its own words and omits its axis labels. Text it writes inside a picture's box was never printed on the page. It is kept for review at a lower confidence, linked to the picture, and is never quotable (DESIGN §3).
- **Check 7a, OCR speed**: at about 20 s a page, OCR is the slow step of ingestion. An 80-page scanned document would take about 27 minutes one page at a time.

## Running one

Every check runs inside a disposable container, started from this folder. This command is the standard from 2026-10-12. It ran for the first time on 2026-10-12, for check 13.

```bash
docker run --rm \
  --user "$(id -u):$(id -g)" \
  -e HOME=/tmp -e UV_CACHE_DIR=/cache/uv \
  -v "$HOME/.cache/uv":/cache/uv \
  -v "$PWD":/spike:ro \
  ghcr.io/astral-sh/uv:python3.12-bookworm-slim \
  uv run /spike/<script> [arguments]
```

What a check adds to it:

- **A PDF:** `-v "$PDF_DIR":/data:ro`, and the script is given `/data/<filing>.pdf`.
- **A key:** `-e OPENROUTER_API_KEY="$(grep '^OPENROUTER_API_KEY=' ../../.env | cut -d= -f2-)"`. Only that key reaches the container, and the command prints nothing.
- **The GPU:** `--gpus all`, with the fonts and libraries of "Check 7a inside a container" below.
- **A service,** such as PostgreSQL: a second container on a private network, and `--network <name>` on the command above. The exact commands are in the script's docstring.

| Script | Arguments | It adds |
|---|---|---|
| `01_langgraph_sync_node_under_async.py` | none | nothing |
| `04_parse_response_size.py` | `/data/<a long filing>.pdf` | a PDF |
| `05_openrouter_image_embedding.py` | `/data/<a filing>.pdf 23 25` | a PDF and the OpenRouter key; under a cent |
| `06_deepeval_judge_openrouter.py` | `/data/<a filing>.pdf 23` | a PDF and the OpenRouter key; under a cent |
| `07a_gpu_docling_lightonocr.py` | `/data/<a long filing>.pdf` | a PDF, the GPU and `-e TORCH_DISABLE_NATIVE_JIT=1` |
| `13_langgraph_postgres_checkpointer.py` | the database URL | a PostgreSQL container on a private network |
| `08_source_downloads.py` | none | the network only; about 60 requests, 80 at most |
| `16_price_providers.py` | none | both free keys, passed with `-e`, and a scratch folder mounted at `/out` for the recorded shapes; about 28 Tiingo requests and 31 Twelve Data credits |

Checks 4 to 7a need a PDF; the recorded runs used Apple's FY2025 10-K from its investor-relations site, where page 23 holds the stock performance chart. The first run of a script downloads its dependencies into uv's cache (about 2.5 GB for the first four, most of it Docling and CPU-only PyTorch; check 7a adds about 5 GB of GPU PyTorch, and the OCR model adds about 2 GB to `~/.cache/huggingface`).

<details>
<summary><b>Check 7a inside a container</b></summary>

The container is disposable. It installs the fonts and libraries as root, then runs the script as your own user, so the two caches it shares with WSL keep their owner. Run it from this folder, with the PDF in `$PDF_DIR`.

```bash
docker run --rm --gpus all \
  -e HOME=/tmp -e UV_CACHE_DIR=/cache/uv -e HF_HOME=/cache/huggingface -e TORCH_DISABLE_NATIVE_JIT=1 \
  -e RUN_UID="$(id -u)" -e RUN_GID="$(id -g)" \
  -v "$(command -v uv)":/usr/local/bin/uv:ro \
  -v "$HOME/.cache/uv":/cache/uv -v "$HOME/.cache/huggingface":/cache/huggingface \
  -v "$PWD":/spike:ro -v "$PDF_DIR":/data:ro \
  ghcr.io/astral-sh/uv:python3.12-bookworm-slim \
  sh -c 'apt-get update -qq && apt-get install -y -qq --no-install-recommends \
           libgl1 libglib2.0-0 libsm6 libxext6 fontconfig fonts-dejavu-core \
         && exec setpriv --reuid="$RUN_UID" --regid="$RUN_GID" --clear-groups \
           uv run /spike/07a_gpu_docling_lightonocr.py /data/<filing>.pdf'
```

</details>
