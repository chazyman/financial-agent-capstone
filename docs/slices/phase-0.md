# Phase 0 slices: environment

Phase 0 delivers a working environment, the engine's empty shell, LightOnOCR running on the laptop's GPU, and the assumptions the design rests on, proved or replaced ([PROPOSAL §5](../../PROPOSAL.md#5-roadmap)). It has no notebook.

Every slice of this phase is a substrate slice. What a slice is, its two kinds, its gate and what happens on a wrong spec are defined once, in [DESIGN §9.2](../DESIGN.md#92-development). Section numbers below refer to DESIGN.md.

## The slices, in order

This table is the phase's queue ([DESIGN §9.2](../DESIGN.md#92-development)): its slices and its assumption checks, in the order they are done. A check's result is in [spikes/phase0/README.md](../../spikes/phase0/README.md#results), and its Status cell links there.

| Step | What exists when it passes | Test | Status |
|---|---|---|---|
| Check 13a, 13b, 13d, 13e | LangGraph's Postgres checkpointer works beside our own stores, a killed run resumes at every level of nesting, and a node can be timed out | Assumption check | [Result](../../spikes/phase0/README.md#results) |
| Check 8 | The Stooq, Stockanalysis and SPDR downloads work within their terms | Assumption check | [Result](../../spikes/phase0/README.md#results) |
| Check 9 | Docling finds the picture boxes on an image-only page | Assumption check | [Result](../../spikes/phase0/README.md#results) |
| Check 11 | A masked trace and its scores reach Langfuse | Assumption check | [Result](../../spikes/phase0/README.md#results) |
| 0.1 Package skeleton | The `uv` environment builds; `finrag_engine` imports; `pytest` with its async plugin and `ruff` run | Unit | Not started |
| 0.2 Settings | `.env`, `settings.yaml` and `sources.yaml` load into one typed `Settings`; a bad value fails at startup | Unit | Not started |
| 0.3 PostgreSQL with pgvector (closes check 10) | The `postgres` service, the first migration, and one async query that filters and ranks by vector distance | Integration | Not started |
| 0.4 Engine shell and the two contexts | An `Engine` built from fakes runs a one-node graph; each run has its own as-of date, clock and budget | Unit and end-to-end on fakes | Not started |
| 0.5 Worker | The `redis` and `worker` services; a queued job runs through the `Engine` | Integration | Not started |
| 0.6 `models` service on the GPU | The image builds with its fonts and system libraries, the container sees the GPU, and `/health` says so | GPU | Not started |
| Check 7b | The reranker and the sentiment classifier fit beside the other two | Assumption check | [Result](../../spikes/phase0/README.md#results) |
| 0.7 Test fixtures | The synthetic layout PDF, the rasterised filing pages with their text-layer twin, and a manifest | Unit | Not started |
| 0.8 LightOnOCR on the laptop GPU | The `models` service transcribes a rasterised page with LightOnOCR on the GPU, and the engine calls it through `OcrEngine` | GPU | Not started |
| 0.9 Durable runs | Every graph the `Engine` compiles is checkpointed in PostgreSQL, and an interrupted run resumes with the budget it had left | Integration | Not started |

<details>
<summary><b>Each slice in detail</b> (9 slices)</summary>

### 0.1 Package skeleton

- **Must hold:** `uv sync` creates the environment from `pyproject.toml` with the groups `dev`, `eval` and `models`. `import finrag_engine` works. `pytest` runs async tests. `ruff` passes. `nbstripout` is set as the git filter for notebooks. `pyproject.toml` pins each library an assumption check ran on to the version the check recorded ([spikes/phase0/README.md](../../spikes/phase0/README.md)); a different version means the check is run again first.
- **Rules:** §8 (project structure), §9.2 (dependencies, markers).
- **Test:** unit. The package imports; the markers `integration`, `gpu` and `network` are registered; an async test runs; no notebook in `notebooks/` carries outputs.

### 0.2 Settings

- **Must hold:** one Pydantic `Settings` object is built from `.env`, `config/settings.yaml` and `config/sources.yaml`. Environment variables override YAML. `.env.example` lists every name with an empty value. `.env` keeps the database URL in its plain form, and `Settings` derives from it the form SQLAlchemy needs to use psycopg 3.
- **Rules:** §10; the source-truth coefficients of §4.
- **Test:** unit. A missing value, a mistyped value and a source without a coefficient each fail at startup, naming the key. An environment variable overrides its YAML value. No secret appears in the object's printed form. The derived database URL names the psycopg 3 driver.

### 0.3 PostgreSQL with pgvector

- **Must hold:** `docker compose up -d postgres` gives a database with the `vector` extension. Alembic applies and reverses the first migration. Alembic's autogenerate skips the schema named by `memory.schema`, so it never proposes to drop LangGraph's tables (check 13a). The engine reaches the database through psycopg 3, the async driver LangGraph's checkpointer also needs (§13.5). One explicit setup step creates the tables, the extension first and then the migrations; it is run by hand and by the test session, never by the engine or the worker. The tests use a database of their own on the same service, which the test session creates and sets up. The stores' pool has a size in the settings, added by this slice.
- **Rules:** §7 ("Exact search first"), §8 ("I/O is async"), §9.1.
- **Test:** integration. The migration upgrades and downgrades cleanly. One async query filters rows with a `WHERE` clause and orders the rest by vector distance, and returns exactly the rows the predicate allows. With a stand-in table in the `memory.schema` schema, autogenerate against the migrated database proposes no change. A test run leaves the working database untouched. The async test plugin runs one event loop for the whole session, because a pool belongs to the loop that created it.
- **Rests on:** check 10 (pgvector through the async driver). This slice's test closes it.

### 0.4 Engine shell and the two contexts

- **Must hold:** `Engine(ctx)` accepts a context built from fakes, and `Engine.from_settings()` builds the real one. `RunContext` carries the run id, the as-of date, the wall clock, the budget (tool calls, model calls, seconds and cost) and the policy level. A graph node receives it through `context_schema`. A run ends in a `RunResult` with a `StopReason`. One wrapper function, applied to every node when a graph is built, carries the node's timeout, its error handler and the copy of what the step spent into the state; each step writes what it spent and the state adds it up. The tracing of the step joins the wrapper in slice 1.7. `from_settings()` allocates nothing: each resource allocates itself on first use, and `aclose()` releases what was allocated in reverse order; `async with` does both. One clock object in the engine context, real or fake, supplies the wall clock.
- **Rules:** §8 ("Two contexts, two lifetimes", "`Engine`", "I/O is async"), §4 ("One as-of date per run", "Two clocks"), §5 ("Every run ends in a typed result").
- **Test:** unit and end-to-end on fakes. A one-node graph run through the engine returns its `RunResult`. Two runs started together with different as-of dates each see their own, and neither waits for the other. A step-limit overrun returns `step_limit` instead of raising, and a run past its time budget returns `timeout`. A node that outlasts `timeouts.node` ends the run with `timeout` and a `RunResult`, not an exception, and the spend of the steps before it is kept. After a two-step graph the state holds the sum of what each step spent. Fake resources are released in the reverse of the order they were allocated, an engine that used none allocates none, and a second `aclose()` does nothing. An architecture test fails if any module other than the clock's reads the system clock.
- **Rests on:** check 1 and check 13d.

### 0.5 Worker

- **Must hold:** `docker compose up -d redis worker` starts an arq worker that calls the engine and never loads agents or skills. The engine enqueues through the `JobQueue` interface, which has a fake.
- **Rules:** §9.1, §8 (interfaces).
- **Test:** integration. A job enqueued from a test is run by the worker and its result is visible. With the fake queue, the same call records the job without Redis.

### 0.6 `models` service on the GPU

- **Must hold:** `docker/models.Dockerfile` builds an image with GPU PyTorch, either a C compiler or `TORCH_DISABLE_NATIVE_JIT=1`, a pinned font set, and OpenCV's system libraries. The container sees the GPU. `/health` reports the device, its free memory and the version of each library and model the image serves, a list that grows as endpoints arrive. No model endpoint yet: `/ocr` arrives in slice 0.8, and the others with the Phase 1 slices that use them.
- **Rules:** §9.1 (the only GPU user; no access to `data/`; "What the `models` image must contain").
- **Test:** GPU. `/health` answers with a CUDA device from inside the container. The engine's client reads it through `MODELS_URL`. Inside the container, the pinned font family is listed and `cv2` imports. `/health` names a version for each thing it serves.
- **Rests on:** check 7a.

### 0.7 Test fixtures

- **Must hold:** `tests/fixtures/` holds a synthetic PDF that reproduces the layouts of §11 "Layout regression"; a few pages of a public SEC filing as a born-digital PDF and as an image-only copy; and a manifest giving each file's hash and what it contains. Nothing in it comes from TipRanks.
- **Rules:** §11 ("Layout regression", "Sample documents"), §2 (TipRanks data in no committed fixture).
- **Test:** unit. Every file matches its hash in the manifest. The born-digital pages have a text layer and the image-only copy has none.

### 0.8 LightOnOCR on the laptop GPU

- **Must hold:** the `models` service gains `/ocr`. It loads LightOnOCR-2-1B on the laptop's GPU on first use and returns the text of a page image. The engine reaches it through the `OcrEngine` interface, which has a fake for tests. The service runs one GPU job at a time, and a request waits its turn. The wait and the model's first load count against the caller's timeout: either the client's timeout allows for them or the service is warmed up first, and this slice settles which. Deciding which pages go to OCR, and masking their pictures, is not here: that is slice 1.13.
- **Rules:** §9.1 (the only GPU user; each model loads on first use); §8 (interfaces); §3 ("OCR routing is a measurement", for what the endpoint will serve).
- **Test:** GPU. `/ocr` on a rasterised fixture page recovers at least 95% of the words of its text-layer twin. The model's GPU memory and the seconds per page are recorded. With the fake `OcrEngine`, the same engine call returns the fake's text with no service running. Two requests sent together are served one after the other and both succeed. The seconds the first call spends loading the model are recorded.
- **Rests on:** check 7a and slices 0.6 and 0.7.

### 0.9 Durable runs

- **Must hold:** every graph the `Engine` compiles gets the PostgreSQL checkpointer, whose tables live in their own schema, outside Alembic. What the budget has spent is part of the graph state, with the as-of date, the policy level and the settings hash the run started with. `engine.resume(run_id)` continues a run from its last completed step, and refuses when the settings hash or the git commit has changed. The setup step of slice 0.3 gains the creation of the `memory.schema` schema and LangGraph's `setup()`, after the migrations, and the checkpointer's pool has a size in the settings. At its first call on a real database the engine checks that the database is at the version the code expects and that the embedding column has the size in the settings, and refuses to start otherwise. The engine runs every graph with `durability="sync"`. A sub-graph keeps its own state and returns its artifact and what it spent; the parent's total counts each spend once. Sessions and the long-term store are not here: they arrive in Phase 5.
- **Rules:** §13.1, §13.5; §5 (`Budget`); §8 (`Engine`, psycopg 3).
- **Test:** integration, on `postgres`. A two-step graph killed at the start of its second step resumes at the second and does not run the first again. The resumed run has the budget it had left. A sub-graph called from a node starts fresh on each call. A sub-graph that spends twice raises the parent's total by exactly two, and a kill inside it still resumes with the right total. A resume under a changed settings hash is refused. An engine started against a database at another version, or with another embedding size in the settings, refuses with a message that names the difference. Unit tests use the in-memory saver.
- **Rests on:** checks 13a and 13b, and slices 0.3 and 0.4.

</details>

## Assumption checks to close in this phase

The checks of this phase are rows of the queue above, each before the first slice that rests on it. The rules of a script and the results are in [spikes/phase0/README.md](../../spikes/phase0/README.md).

Three checks wait for a later phase, and each is queued when that phase is sliced:

- **Check 14** (the MCP SDK under the TipRanks client): Phase 2, before its first slice that fetches from a provider. The SDK's documentation says nothing about retries or about how a refusal surfaces.
- **Check 12** (Ragas): Phase 4.
- **Check 13c** (LangGraph's Postgres store): Phase 5.

## Closing the phase

- Every slice has passed its gate, and `pytest` and `pytest -m integration` pass together.
- `docker compose up -d` brings up `postgres`, `redis`, `worker` and `models`.
- LightOnOCR transcribes the rasterised fixture pages on the laptop's GPU, inside the `models` container.
- A run killed between two steps resumes at the next one (slice 0.9).
- Every check in the queue has passed, or its fallback has been written into DESIGN.md.
