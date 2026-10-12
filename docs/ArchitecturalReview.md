# Architectural Review

A review held before the first coding session, started on 2026-10-09. It checks the design against the goal and the effort, and records one decision per topic. The design stays in [DESIGN.md](DESIGN.md): a decision made here takes effect only when it is written there or in a slice file.

## Ground rules

- **Scope:** what the code of Phases 0 and 1 will lock in, and what the later phases have already pulled into those two.
- **Each topic ends in one decision:** keep, change, defer or cut.
- **No new detail for a phase that is not sliced.** A finding about one is parked in its topic here, not written into DESIGN.md.

## Topics

| # | Topic | Decision | Status |
|---|---|---|---|
| 1 | The status of the Phase 2 to 5 detail in DESIGN.md | B: two tiers; a draft is compacted by moving when work on its phase starts | Decided 2026-10-09, written in DESIGN §9.3 |
| 2 | Durable runs in Phase 0 (slice 0.9, check 13) | Keep in Phase 0: it is infrastructure | Decided 2026-10-09 |
| 3 | The worker and Redis in Phase 0 (slice 0.5) | Keep in Phase 0: the infrastructure is tested as code is added | Decided 2026-10-09 |
| 4 | Run controls in Phases 0 and 1 (slices 0.4 and 1.6) | Keep where they are, for the same reason | Decided 2026-10-09 |
| 5 | The lock-ins of Phase 0: the two contexts, async on psycopg 3, the database split, the `models` service boundary, the `Engine` API | Keep all five; a lifecycle is added to `Engine` | Decided 2026-10-09; written in the slice files and DESIGN §7 and §8 |
| 6 | What the capstone includes: the agents, the provider, memory | Keep all: nothing is cut or deferred | Decided 2026-10-09 |
| 7 | News that is not about one instrument: market and economic news, and items about several | Store, link and show as context; no modelling of impact | Decided 2026-10-09; the Phase 1 part is written in DESIGN §7 and slices 1.1 and 1.3, the Phase 2 part is parked |
| 8 | The workflow around slices and assumption checks: where status and the order of work are written, and the loop of a check | Change: each fact has one home, and a check gets a loop of its own | Decided 2026-10-12; written in DESIGN §9.2, PROPOSAL §6, the slice files and `spikes/phase0/README.md` |


## 1. The status of the Phase 2 to 5 detail

**Question.** DESIGN.md specifies Phases 2 to 5 to implementation level, and only Phases 0 and 1 are sliced. Is that text binding now?

**What was found.**
- By estimate, a bit over a third of DESIGN.md serves only Phases 2 to 5: most of §5, §6, the provider part of §2, and the sessions and long-term memory of §13.
- The later phases already shape the first two. Slice 0.9 builds the durable runs of §13. Slice 0.5 builds the worker, whose scheduled refresh is a Phase 2 deliverable. Slices 0.4 and 1.6 build the run controls of §5.
- The slice files already say which rules a slice relies on, in each slice's Rules line.

**Options.**
- **A. All of it is binding,** as today. The review must then cover every section.
- **B. Two tiers.** A rule is binding when a slice's Rules line cites it, when it is a design invariant, or when it is one of the seams of §9 ("Built to be extended"). Everything else is a draft until its phase is sliced. It is then read again against what the code has taught, corrected once, and becomes binding.
- **C. Move the draft out** of DESIGN.md into one file per phase. The document shrinks by a third now, at the cost of a restructuring session and of every section number that other files cite.

**Recommendation.** B. It needs one sentence in DESIGN §9.3 and no rewrite, and it limits this review to topics 2 to 6.

**Decision.** B, on 2026-10-09, written in DESIGN §9.3 ("What is binding"). A draft is compacted when work on its phase starts, by moving and not by summarising: each rule keeps its statement and its reason in DESIGN.md, its detail moves to the Must hold and Test lines of the slice that builds it, and what the code has shown to be wrong is dropped with its reason. Phase 1 is already sliced, so it is compacted when its work starts and not before. For Phase 0, this review was that reading: its corrections are the points of topic 5, and its blocks retire as its slices land. The invariant lines of `CLAUDE.md` keep their own change procedure.

## 5. The lock-ins of Phase 0

**Question.** Five choices are expensive to change once code exists. Is each right?

**What was found.**

| Lock-in | Verdict | Evidence | To settle, and where |
|---|---|---|---|
| The two contexts | Keep | Check 1 passed: two runs each saw their own context | Slice 0.4: how the budget's count gets from the gateway and the chat-model wrapper, which are not nodes, into the graph state |
| Async on psycopg 3 | Keep | Checks 10 and 13 are not run yet. Both fallbacks keep the async store interface, so the choice survives either one failing | Slices 0.2 and 0.3: one `DATABASE_URL` serves two libraries that spell the driver differently and need separate pools |
| The database split | Keep | None needed | Slice 0.3: the tests need a database of their own on the same service, so a test run never touches the corpus the notebooks use |
| The `models` service boundary | Keep | Check 7a: 80 pages parsed in 30 s inside a container. Check 4: an answer of 7.7 MB | Slice 1.13: OCR takes about 20 s a page, so a scanned document of more than about 11 pages passes `timeouts.node` if one node reads all of it |
| The `Engine` API | Keep, with one addition | None needed | Slice 0.4 and DESIGN §8: the API has no way to open and close the pools and clients it holds |

**Verdicts, one lock-in at a time.**
- **The two contexts: keep** (2026-10-09). One wrapper around every node, a function applied when a graph is built, carries the node timeout, the error handler, the tracing of the step and the copy of what the step spent into the state. Each step writes what it spent and the state adds it up, so parallel steps stay possible; steps run in sequence for now. The count needs no lock: it is counted in async code and reserved before the call. A base class for nodes was considered and not chosen: the same behaviour, a rule of DESIGN §8 to rewrite and one more assumption to check. Written in slice 0.4; the tracing of the step joins the wrapper in slice 1.7.
- **Async on psycopg 3: keep** (2026-10-09). Four points settled: `httpx` replaces `requests`, a blocking library, in PROPOSAL §8.2; `.env` keeps the plain database URL and `Settings` derives the form SQLAlchemy needs (slice 0.2); the stores' pool and the checkpointer's pool each get a size in the settings (slices 0.3 and 0.9); the async test plugin uses one event loop per session, because a pool belongs to the loop that created it (slice 0.3). Parked for the close of Phase 1: DeepEval and Ragas run their own event loop, which a notebook already has.
- **The database split: keep** (2026-10-09). Settled: the tests get a database of their own on the same service, created and migrated by the test session (slice 0.3); a document's rows are committed in one transaction, and `ingest_runs` is written by the ingestion sub-graph in that transaction, which corrects the one sentence of DESIGN §7 that says otherwise (slice 1.1); a startup check compares the embedding size in the settings with the column's (slice 0.9, the first slice where the engine meets a real database); outside the `instruments` table and the lookup, no code assumes `instrument_id` is a ticker (slice 1.1); and the two rules of topic 7. Parked for Phase 1: a uniqueness rule on a document's content hash, so a document shared by several instruments is stored once, and a crop file written before the row that points to it. Accepted: the ticker stays the key.
- **The `models` service boundary: keep** (2026-10-09). Settled: the service runs one GPU job at a time and a request waits its turn (slice 0.8); the wait and a model's first load count against the caller's timeout, so the client allows for them or the service offers a warm-up call (slice 0.8); `/health` returns the versions of Docling and of each model, and ingestion records those in `ingest_runs` (slices 0.6 and 1.9). Parked for Phase 1: how the `/parse` answer packs its JSON and image files (slice 1.9).
- **The `Engine` API: keep, with a lifecycle added** (2026-10-09). Settled:
  - *Lifecycle.* Each resource class allocates and releases itself, and the engine composes them. `from_settings()` allocates nothing. The database pools are allocated at the first engine call, with the startup checks; every other resource allocates on its first use. `aclose()` releases what was allocated, in reverse order, traces flushed first, and `async with` does both (slice 0.4 for the lifecycle, slice 0.9 for the checks on a real database, and one clause in DESIGN §8).
  - *Tables.* One explicit setup step creates them: the extension and the schema, the migrations, then LangGraph's `setup()`. It is run by hand and by the test session, never by the worker or the engine. The startup checks include the database's version (slices 0.3 and 0.9).
  - *Clock.* One clock object in the engine context, real or fake, supplies the wall clock, and the architecture test allows only its module to read the system clock (slice 0.4).
  - Accepted: `answer` and `ask` are close names for two paths, and both stay.

**Decision.** All five are kept (2026-10-09). Nothing changes direction, and no slice is added: the points above go into the Must hold and Test lines of existing slices. Written on 2026-10-09.

## 7. News that is not about one instrument

**Question.** A document, a chunk and a news item carry exactly one instrument. Market and economic news has none, and some items concern several. Is that a gap?

**Decision** (2026-10-09). Yes, and it is closed at one level. The tradable universe stays ETFs and equities: no bonds, options or crypto.
- **Kept: store, link and show as context.** Market news is in the corpus. An answer about an instrument also shows the market, sector and co-mentioned news, labelled as such. The system reports what a source says about impact and never works impact out itself.
- **Dropped:** a map of which company affects which, and any measured or predicted impact.

**A lock-in of Phase 1,** to write in DESIGN §7 and in slices 1.1 and 1.3:
- A document, a chunk and a news item link to zero, one or several instruments. A typed record stays on one.
- `type` is an open set, and a type's sources and metrics come from config.

**Parked until Phase 2 is sliced:**
- One row of type `Market` in the universe file, which market news belongs to.
- A market news source: primary releases and a search by topic, with their event types.
- Context added in code: an instrument's news window also covers its sector ETF and the market row.
- A context block in the news digest, with a cap per answer.
- One line in PROPOSAL §3 for the new source.
- DESIGN §6 still says an item is tagged to a ticker; it follows the link when Phase 2 is sliced.
- Two limits, accepted: a link across sectors depends on an article naming both instruments, and the provenance check does not verify that a cited article states a cause.

## 8. The workflow around slices and assumption checks

**Question.** At the first code session (2026-10-11) the open items and `spikes/phase0/README.md` disagreed on when check 13 runs. Is that one stale line, or the process?

**What was found.** The process.
- A check's status and the order of work were written in about ten places in five files: the spike README, `slices/phase-0.md`, DESIGN.md, the course map and the open items.
- A check had no loop of its own. Nothing said who approves its criterion, what a result must update, or what its commit holds.
- No rule said when a check runs. For checks 9 and 11 and the rest of check 7, the slice file said before Phase 0 closes, and the spike README said before slices 1.13, 1.7 and 1.16.
- One check number held several assumptions. Check 13 had four, serving slice 0.9, slice 0.4 and Phase 5, and check 7 was "half done".
- The versions the checks ran on were not tied to the lockfile of slice 0.1.

**Decision** (2026-10-11 and 2026-10-12). Change, in eight points:
- The queue of a phase is the table of its slice file: its slices and its checks, in the order they are done. It is the only place that states that order and a slice's status.
- A check's result is written in the spike README only. Its row in the queue gives its position and a link.
- A check follows a loop: its criterion approved before the run, the run, the record, and one commit of its own.
- The checks that gate Phase 1 slices run in Phase 0, as early as what each needs exists.
- One result per assumption. Check 13 becomes 13a to 13d, and check 7 becomes 7a and 7b.
- 13a covers the checkpointer only. Everything about the long-term store, its tables included, waits with 13c until Phase 5 is sliced.
- `pyproject.toml` pins each library a check ran on to the version the check recorded (slice 0.1).
- The spike README's schedule, its Status paragraph and its "What is left" table, is removed, and the status words in the other files with it.

Written on 2026-10-12.
