# Phase 0 slices: environment

Phase 0 delivers a working environment, the engine's empty shell, LightOnOCR running on the laptop's GPU, and the assumptions the design rests on, proved or replaced ([PROPOSAL §5](../../PROPOSAL.md#5-roadmap)). It has no notebook.

Every slice of this phase is a substrate slice. What a slice is, its two kinds, its gate and what happens on a wrong spec are defined once, in [DESIGN §9.2](../DESIGN.md#92-development). Section numbers below refer to DESIGN.md.

## The slices, in order

| Slice | What exists when it passes | Test | Status |
|---|---|---|---|
| 0.1 Package skeleton | The `uv` environment builds; `finrag_engine` imports; `pytest` with its async plugin and `ruff` run | Unit | Not started |
| 0.2 Settings | `.env`, `settings.yaml` and `sources.yaml` load into one typed `Settings`; a bad value fails at startup | Unit | Not started |
| 0.3 PostgreSQL with pgvector | The `postgres` service, the first migration, and one async query that filters and ranks by vector distance | Integration | Not started |
| 0.4 Engine shell and the two contexts | An `Engine` built from fakes runs a one-node graph; each run has its own as-of date, clock and budget | Unit and end-to-end on fakes | Not started |
| 0.5 Worker | The `redis` and `worker` services; a queued job runs through the `Engine` | Integration | Not started |
| 0.6 `models` service on the GPU | The image builds with its fonts and system libraries, the container sees the GPU, and `/health` says so | GPU | Not started |
| 0.7 Test fixtures | The synthetic layout PDF, the rasterised filing pages with their text-layer twin, and a manifest | Unit | Not started |
| 0.8 LightOnOCR on the laptop GPU | The `models` service transcribes a rasterised page with LightOnOCR on the GPU, and the engine calls it through `OcrEngine` | GPU | Not started |
| 0.9 Durable runs | Every graph the `Engine` compiles is checkpointed in PostgreSQL, and an interrupted run resumes with the budget it had left | Integration | Not started |

<details>
<summary><b>Each slice in detail</b> (9 slices)</summary>

### 0.1 Package skeleton

- **Must hold:** `uv sync` creates the environment from `pyproject.toml` with the groups `dev`, `eval` and `models`. `import finrag_engine` works. `pytest` runs async tests. `ruff` passes. `nbstripout` is set as the git filter for notebooks.
- **Rules:** §8 (project structure), §9.2 (dependencies, markers).
- **Test:** unit. The package imports; the markers `integration`, `gpu` and `network` are registered; an async test runs; no notebook in `notebooks/` carries outputs.

### 0.2 Settings

- **Must hold:** one Pydantic `Settings` object is built from `.env`, `config/settings.yaml` and `config/sources.yaml`. Environment variables override YAML. `.env.example` lists every name with an empty value.
- **Rules:** §10; the source-truth coefficients of §4.
- **Test:** unit. A missing value, a mistyped value and a source without a coefficient each fail at startup, naming the key. An environment variable overrides its YAML value. No secret appears in the object's printed form.

### 0.3 PostgreSQL with pgvector

- **Must hold:** `docker compose up -d postgres` gives a database with the `vector` extension. Alembic applies and reverses the first migration. The engine reaches the database through psycopg 3, the async driver LangGraph's checkpointer also needs (§13.5).
- **Rules:** §7 ("Exact search first"), §8 ("I/O is async"), §9.1.
- **Test:** integration. The migration upgrades and downgrades cleanly. One async query filters rows with a `WHERE` clause and orders the rest by vector distance, and returns exactly the rows the predicate allows.
- **Rests on:** check 10 (pgvector through the async driver). This slice's test closes it.

### 0.4 Engine shell and the two contexts

- **Must hold:** `Engine(ctx)` accepts a context built from fakes, and `Engine.from_settings()` builds the real one. `RunContext` carries the run id, the as-of date, the wall clock, the budget (tool calls, model calls, seconds and cost) and the policy level. A graph node receives it through `context_schema`. A run ends in a `RunResult` with a `StopReason`.
- **Rules:** §8 ("Two contexts, two lifetimes", "`Engine`", "I/O is async"), §4 ("One as-of date per run", "Two clocks"), §5 ("Every run ends in a typed result").
- **Test:** unit and end-to-end on fakes. A one-node graph run through the engine returns its `RunResult`. Two runs started together with different as-of dates each see their own, and neither waits for the other. A step-limit overrun returns `step_limit` instead of raising, and a run past its time budget returns `timeout`. An architecture test fails if any module of the package reads the system clock.
- **Rests on:** check 1 (passed).

### 0.5 Worker

- **Must hold:** `docker compose up -d redis worker` starts an arq worker that calls the engine and never loads agents or skills. The engine enqueues through the `JobQueue` interface, which has a fake.
- **Rules:** §9.1, §8 (interfaces).
- **Test:** integration. A job enqueued from a test is run by the worker and its result is visible. With the fake queue, the same call records the job without Redis.

### 0.6 `models` service on the GPU

- **Must hold:** `docker/models.Dockerfile` builds an image with GPU PyTorch, either a C compiler or `TORCH_DISABLE_NATIVE_JIT=1`, a pinned font set, and OpenCV's system libraries. The container sees the GPU. `/health` reports the device and its free memory. No model endpoint yet: `/ocr` arrives in slice 0.8, and the others with the Phase 1 slices that use them.
- **Rules:** §9.1 (the only GPU user; no access to `data/`; "What the `models` image must contain").
- **Test:** GPU. `/health` answers with a CUDA device from inside the container. The engine's client reads it through `MODELS_URL`. Inside the container, the pinned font family is listed and `cv2` imports.
- **Rests on:** check 7a (Docling and LightOnOCR passed inside a GPU container on 2026-10-04, once fonts and OpenCV's libraries were installed). The reranker and the sentiment classifier are still to measure; that is the rest of check 7, run in this image once it exists.

### 0.7 Test fixtures

- **Must hold:** `tests/fixtures/` holds a synthetic PDF that reproduces the layouts of §11 "Layout regression"; a few pages of a public SEC filing as a born-digital PDF and as an image-only copy; and a manifest giving each file's hash and what it contains. Nothing in it comes from TipRanks.
- **Rules:** §11 ("Layout regression", "Sample documents"), §2 (TipRanks data in no committed fixture).
- **Test:** unit. Every file matches its hash in the manifest. The born-digital pages have a text layer and the image-only copy has none.

### 0.8 LightOnOCR on the laptop GPU

- **Must hold:** the `models` service gains `/ocr`. It loads LightOnOCR-2-1B on the laptop's GPU on first use and returns the text of a page image. The engine reaches it through the `OcrEngine` interface, which has a fake for tests. Deciding which pages go to OCR, and masking their pictures, is not here: that is slice 1.13.
- **Rules:** §9.1 (the only GPU user; each model loads on first use); §8 (interfaces); §3 ("OCR routing is a measurement", for what the endpoint will serve).
- **Test:** GPU. `/ocr` on a rasterised fixture page recovers at least 95% of the words of its text-layer twin. The model's GPU memory and the seconds per page are recorded. With the fake `OcrEngine`, the same engine call returns the fake's text with no service running.
- **Rests on:** check 7a (passed inside a GPU container on 2026-10-04: about 20 s a page, 2.5 GB held together with Docling, 5.3 GB at peak) and slices 0.6 and 0.7.

### 0.9 Durable runs

- **Must hold:** every graph the `Engine` compiles gets the PostgreSQL checkpointer, whose tables live in their own schema, outside Alembic. What the budget has spent is part of the graph state, with the as-of date, the policy level and the settings hash the run started with. `engine.resume(run_id)` continues a run from its last completed step, and refuses when the settings hash or the git commit has changed. Sessions and the long-term store are not here: they arrive in Phase 5.
- **Rules:** §13.1, §13.5; §5 (`Budget`); §8 (`Engine`, psycopg 3).
- **Test:** integration, on `postgres`. A two-step graph killed after its first step resumes at the second and does not run the first again. The resumed run has the budget it had left. A sub-graph called from a node starts fresh on each call. A resume under a changed settings hash is refused. Unit tests use the in-memory saver.
- **Rests on:** check 13, and slices 0.3 and 0.4.

</details>

## Assumption checks to close in this phase

The scripts, their results and the reasons for the order below are in [spikes/phase0/README.md](../../spikes/phase0/README.md). Checks 1 to 6 and 7a have passed. Check 13 must pass before slice 0.9; none of the other open checks blocks a slice of this phase. Check 12 (Ragas) waits for Phase 4.

| Priority | Check | Needs | Blocks |
|---|---|---|---|
| 1 | 10. pgvector through the async driver | The `postgres` service | Closed by slice 0.3 |
| 2 | 13. LangGraph's Postgres checkpointer and store work beside our own stores | The `postgres` service | Slice 0.9 |
| 3 | 8. The Stooq, Stockanalysis and SPDR downloads work within their terms | Network | Slice 1.14 |
| 4 | 11. A masked trace and its scores reach Langfuse | Langfuse keys | Slice 1.7 |
| 5 | 9. Docling finds the picture boxes on an image-only page | The GPU | Slice 1.13 |
| 6 | 7, the rest. The reranker and the sentiment classifier fit beside the other two | The `models` image of slice 0.6 | Slice 1.16 |

## Closing the phase

- Every slice has passed its gate, and `pytest` and `pytest -m integration` pass together.
- `docker compose up -d` brings up `postgres`, `redis`, `worker` and `models`.
- LightOnOCR transcribes the rasterised fixture pages on the laptop's GPU, inside the `models` container.
- A run killed between two steps resumes at the next one (slice 0.9).
- Every check above has passed, or its fallback has been written into DESIGN.md.
