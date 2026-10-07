# Phase 0 checks

Each script here proves or disproves one assumption the design rests on, before any engine code depends on it.

- **Isolated.** A script declares its own dependencies in its header and runs in a disposable environment: `uv run <script>`. Nothing is installed into the project or the system; `uv cache clean` removes what was downloaded.
- **Criterion first.** The pass criterion is written in the script's docstring before the script is run, and the script prints `RESULT: PASS` or `RESULT: FAIL` against it, with the library versions it used.
- **A fallback for each.** A failure changes a line in the design; it does not stall the build.
- **No secrets printed, no metered calls.** Keys are read from `.env`.
- **A dated record, not a test.** A result holds for the versions and the date shown. Re-run a script when its dependency is upgraded. A script's file number is its check number.

## Status

Checks 1 to 6 and 7a have passed. Check 7 is half done, and checks 8 to 13 are not run. Check 13 must pass before slice 0.9; none of the other open checks blocks a Phase 0 slice.

## Results

| # | Assumption | Needs | Result | Evidence | If it fails |
|---|---|---|---|---|---|
| 1 | LangGraph runs a sync node in a thread when the graph is run async, and each run sees its own run context | Nothing | **Pass** (2026-10-03) | langgraph 1.2.12. Two runs with a 1 s blocking sync node finished in 1.04 s and the event loop kept ticking | Make every node async |
| 2 | A scripted fake chat model can return tool calls and structured output, and goes through the budget and fallback wrapper | Nothing | **Pass** (2026-10-03) | langchain-core 1.6.6. The stock fake raises `NotImplementedError` for both; a 25-line fake of our own passes, sync and async | Write a larger fake |
| 3 | Which host variable the Langfuse SDK reads | Nothing | **Pass, with a change** (2026-10-03) | langfuse 4.16.0 reads both. `LANGFUSE_BASE_URL` wins when both are set, and `LANGFUSE_HOST` is marked deprecated | Rename one variable |
| 4 | A parse answer carrying every crop of a long filing stays a workable size | Docling on CPU | **Pass, with a change** (2026-10-03) | docling 2.133.0 on Apple's 80-page FY2025 10-K: 7.7 MB in total (52 table and 2 picture crops 4.9 MB, JSON 2.5 MB, Markdown 0.35 MB); 102 s on CPU | Compress the answer, or keep a shared folder |
| 5 | OpenRouter embeds an image with Gemini Embedding 2, in the same space as text | OpenRouter key | **Pass** (2026-10-04) | `google/gemini-embedding-2` returns normalised vectors of 1536 values for a text, a PNG, and a crop with its text together, with `data_collection: deny`. A question ranks the right page image first (0.49 against 0.34). 258 tokens an image; $0.0004 for six items | The local embedder named in PROPOSAL §8.1 |
| 6 | A DeepEval judge on OpenRouter separates a correct answer from one with a wrong figure, by more than its own run-to-run noise; it scores a case whose context is an image; what a run costs | OpenRouter key | **Pass** (2026-10-04) | deepeval 4.2.8, judge `google/gemini-2.5-flash-lite`. The correct answer scored 1.0 three times on faithfulness and on G-Eval; the wrong figure scored 0.0 on both. The image case scored 1.0 against 0.0. 24 s, $0.0006 | Judge text only; chart cases rely on hard checks |
| 7 | The four GPU models fit in 16 GB inside the `models` container | Docker and the GPU | **Half done** (2026-10-04): Docling and LightOnOCR pass inside a GPU container | See "Check 7a" below. The reranker and the sentiment classifier are not measured | `models_service.idle_unload_seconds` |
| 8 | The Stooq, Stockanalysis and SPDR downloads work within their terms | Network | Not run | | Alternative sources |
| 9 | Docling's layout model finds the picture boxes on an image-only page, so they can be masked before OCR | The GPU | Not run | | OCR each layout region separately |
| 10 | pgvector queries with SQL predicates, and a full-text candidate list under the same predicates, work through psycopg 3 from async SQLAlchemy | The `postgres` container | Not run | | Sync driver wrapped in a thread, behind the same async store methods |
| 11 | A masked trace and its scores reach Langfuse, tagged with the slice and the git commit | Langfuse keys | Not run | | The run record in PostgreSQL alone; Langfuse stays optional |
| 12 | Ragas' `TestsetGenerator` produces usable questions from our documents through OpenRouter | OpenRouter key; needed at the start of Phase 4 | Not run | | DeepEval's `Synthesizer` |
| 13 | LangGraph's Postgres checkpointer and store work beside our own stores: on psycopg 3, in their own schema, untouched by Alembic. A graph with a sub-graph and a run context resumes after its process is killed, without repeating a completed step. The store's semantic search runs at our embedding dimensions. A node timeout exists on our LangGraph version | The `postgres` container | Not run | | The run record's `run_steps` as the resume log, and tables of our own for preferences and run records; `asyncio.timeout` in a node wrapper |

## What is left, in priority order

| Priority | Check | When | Why this order |
|---|---|---|---|
| 1 | 10. pgvector through the async driver | With slice 0.3, in Phase 0 | It is that slice's own test, and every store depends on the driver |
| 2 | 13. LangGraph's checkpointer and store in PostgreSQL | With the container of slice 0.3, before slice 0.9 | Checkpointing and memory are required (DESIGN §13), and the driver chosen in slice 0.3 has to suit them |
| 3 | 8. The three downloads | At the start of the next session. Needs only the network | It costs nothing, and a failure changes the data sources (DESIGN §1, §2). Blocks slice 1.14 |
| 4 | 11. A masked trace reaches Langfuse | Before slice 1.7. Needs the Langfuse keys in `.env` | Slice 1.7 is the first Phase 1 slice that waits on a check. A failure costs little: the run record is in PostgreSQL |
| 5 | 9. Docling finds picture boxes on an image-only page | Before slice 1.13. Can run now that containers reach the GPU | The rule that keeps a model's reading of a picture out of the text rests on it (DESIGN §3) |
| 6 | 7, the rest. Reranker and sentiment classifier, all four models loaded | Once the `models` image exists (slice 0.6), before slice 1.16 | Low risk: two models peak at 5.3 GB of 16 GB |
| 7 | 12. Ragas generates questions | At the start of Phase 4 | Nothing needs it before the question set is generated |

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

```bash
uv run 01_langgraph_sync_node_under_async.py
uv run 04_parse_response_size.py <path to a long filing PDF>
OPENROUTER_API_KEY=... uv run 05_openrouter_image_embedding.py <path to a filing PDF> 23 25    # under a cent
OPENROUTER_API_KEY=... uv run 06_deepeval_judge_openrouter.py <path to a filing PDF> 23       # under a cent
TORCH_DISABLE_NATIVE_JIT=1 uv run 07a_gpu_docling_lightonocr.py <path to a long filing PDF>   # needs the GPU
```

Checks 4 to 7a need a PDF; the runs above used Apple's FY2025 10-K from its investor-relations site, where page 23 holds the stock performance chart. The first run of a script downloads its dependencies into uv's cache (about 2.5 GB for the first four, most of it Docling and CPU-only PyTorch; check 7a adds about 5 GB of GPU PyTorch, and the OCR model adds about 2 GB to `~/.cache/huggingface`).

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
