# Phase 1 slices: multimodal RAG

Phase 1 delivers the ingestion pipeline, filtered and reranked retrieval, a first answer graph, the run record, and a behaviour baseline per question ([PROPOSAL §5](../../PROPOSAL.md#5-roadmap)). Its notebooks are `01_ingestion` and `02_retrieval`.

Every slice of this phase is a vertical slice. What a slice is, its two kinds, its gate and what happens on a wrong spec are defined once, in [DESIGN §9.2](../DESIGN.md#92-development). Section numbers below refer to DESIGN.md.

**The order is deliberate.** Slices 1.1 to 1.8 build a thin path from a plain text document to a cited answer, and end with the baseline that measures it. Slices 1.9 to 1.16 then add real documents and better ranking. Each of those changes what a model sees, so each is measured against that baseline at its gate.

## The slices, in order

| Slice | Entry point | What must hold when it passes | Test | Status |
|---|---|---|---|---|
| **A. A thin path, text only** | | | | |
| 1.1 Universe and first stored document | `engine.ingest()` | A text item becomes a document, a chunk and an ingestion record | End-to-end | Not started |
| 1.2 Embedding | `engine.ingest()` | Every chunk is stored with its vector | End-to-end | Not started |
| 1.3 Search with predicates | `engine.retrieve()` | Each predicate, the as-of date included, excludes what it should before any candidate is scored; an exact string is found by full-text search | End-to-end | Not started |
| 1.4 Sufficiency gate and evidence set | `engine.retrieve()` | Too little evidence stops the run and names what is missing | Unit and end-to-end | Not started |
| 1.5 First answer graph | `engine.answer()` | A question returns claims that each cite retrieved evidence, or a stop with its reason | End-to-end | Not started |
| 1.6 Run controls around the chat model | `engine.answer()` | The budget, the model fallback and the retry after an invalid output act on any chat model, a fake included, and a run they stop ends with its reason | End-to-end | Not started |
| 1.7 Run record and tracing | `engine.answer()` | Every run is stored in PostgreSQL and sent, masked, to Langfuse | End-to-end | Not started |
| 1.8 Behaviour baseline | evaluation run | Each question has a baseline, and a later run is compared with it | Unit and evaluation | Not started |
| **B. Real documents** | | | | |
| 1.9 Parse a born-digital PDF | `engine.ingest()` | Typed elements with their page, box and crop reach the store | End-to-end | Not started |
| 1.10 Text chunks under their heading path | `engine.ingest()` | Text is chunked under headings, a long section in pieces that stand alone; footnotes stay with their text; furniture is dropped; damage is counted | Unit and end-to-end | Not started |
| 1.11 Tables | `engine.ingest()` | A table is stored as Markdown or as label-value lines, after the sanity checks: signs, scale, subtotals and footnotes included | Unit and end-to-end | Not started |
| 1.12 Pictures and text printed in charts | `engine.ingest()` | Decorative pictures cost nothing; printed chart text is a linked, quotable chunk | Unit and end-to-end | Not started |
| 1.13 OCR routing and OCR | `engine.ingest()` | Scanned pages go to OCR with their pictures masked; born-digital pages don't | Unit and end-to-end | Not started |
| 1.14 Acquisition without an agent | `engine.refresh()` | Configured sources and a supplied file are fetched, deduplicated and ingested | End-to-end | Not started |
| 1.15 A refresh never destroys | `engine.ingest()` | Changed content becomes a new version; the old one is superseded, not deleted | End-to-end | Not started |
| **C. Better ranking** | | | | |
| 1.16 Normalisation, reranking and source truth | `engine.retrieve()` | Tables and charts reach the top results; a filing outranks a weaker source of equal relevance | End-to-end | Not started |

<details>
<summary><b>A. A thin path, text only</b> (slices 1.1 to 1.8)</summary>

### 1.1 Universe and first stored document

- **Must hold:** `data/raw/ListEquities.csv` loads into `instruments`, 19 rows. Ingesting one plain text item for a ticker writes a `documents` row, one text chunk with its envelope, and an `ingest_runs` row holding its counts and the git commit.
- **Rules:** §1; §7 (corpus tables, chunk envelope, "Every row says when it was known"); §12.2.
- **Test:** end-to-end on fakes and `postgres`. The rows exist and point to each other. This slice also adds the database invariants every later end-to-end test reuses: every chunk and record points to its ingestion run, counts match `ingest_runs`, nothing is orphaned.

### 1.2 Embedding

- **Must hold:** each chunk is embedded through the `Embedder` interface, as text, as a crop, or as both together, and stored in `chunks.embedding`. `ingest_runs` records the embedding model.
- **Rules:** §3 ("Visual elements are embedded as crop + text together"); §8 (interfaces are multimodal by contract).
- **Test:** end-to-end with the fake embedder. Every chunk has a vector of the configured size.
- **Rests on:** check 5 (passed).

### 1.3 Search with predicates

- **Must hold:** the predicates are applied as a `WHERE` clause, and two candidate lists are drawn from what survives: by vector similarity, and by full-text search over the text that was embedded, up to `retrieval.candidates_k` each. Predicates: ticker, document type, filing Item or section, `require_checkable`, the as-of date on `published_at`, superseded versions, and `injection_suspect`.
- **Rules:** §4 ("Predicates come before similarity", "A section can be a predicate", "Two candidate lists, one pool", "Known by then, or not admissible"); §7 ("Exact search first").
- **Test:** end-to-end on a seeded corpus with planted vectors. For each predicate, a chunk that is the closest match but fails the predicate is in neither list. A chunk published after the as-of date is not returned. A chunk holding an exact string the query names is in the pool even when its vector ranks outside it.
- **Rests on:** check 10, closed by slice 0.3.

### 1.4 Sufficiency gate and evidence set

- **Must hold:** `engine.retrieve()` returns an `EvidenceSet`. Each hit carries its kind, representation, anchor, extraction method, CHECKABLE or ESTIMATED tag, dates, and source with its truth coefficient. With fewer than `retrieval.min_hits` candidates, the run stops with `insufficient_evidence` and says what is missing.
- **Rules:** §4 (modality normalisation makes the gate count candidates; "Every citation carries its dates"); §5 ("Hits keep their modality").
- **Test:** unit for the gate, which is a control. End-to-end: a question the corpus can't answer stops with its reason; one it can answer returns hits with every field filled.

### 1.5 First answer graph

- **Must hold:** `engine.answer(question, as_of)` retrieves, drafts a `Draft` of claims as structured output, and returns them with citations: source, anchor, dates, source-truth coefficient, and the as-of date in the footer. Every claim cites a retrieved chunk. The scripted chat model for tests exists, and the graph calls whichever chat model the context holds. The run controls around that model are not here: they arrive in slice 1.6. Nor is the provenance gate, which arrives in Phase 3.
- **Rules:** §4 (as-of rules, "Every citation shows its source truth"); §5 (`draft_claims`, `RunResult`, `StopReason`); §7 (a claim's source is flat for the model, typed for the verifier); §8 (the chat model's fake is a scripted model).
- **Test:** end-to-end with the scripted chat model. A scripted draft comes back with resolved citations. A claim citing a chunk that was not retrieved is rejected. Two questions asked at once each get their own answer and neither blocks the other.
- **Rests on:** check 2 (passed).

### 1.6 Run controls around the chat model

- **Must hold:** one wrapper around any `BaseChatModel` counts model calls and cost against the run's `Budget`, switches to the fallback model on failure, and reports each call to the recorder. A structured output that fails its schema is asked for again with the error, up to `policy.max_validation_retries` times per model, and the run then stops with `invalid_output`. A failed call is retried as its failure class allows. The context holds the wrapped model, so the answer graph of slice 1.5 goes through the wrapper unchanged, and so does the scripted chat model.
- **Rules:** §5 ("`Budget`", "Model fallback", "A model output that fails its schema"); §4 ("Retries follow the failure class"); §8 ("Run controls wrap the chat model").
- **Test:** end-to-end at `engine.answer()` with the scripted chat model. A run whose model calls exceed the limit ends in `budget_exhausted`. A scripted primary that fails hands over to the fallback, the question is still answered, and the switch is recorded. A scripted model that returns an invalid `Draft`, then a valid one, answers within the limit; one that never returns a valid one ends in `invalid_output`. A transient failure is retried and a refusal is not.
- **Rests on:** check 2 (passed) and slice 1.5.

### 1.7 Run record and tracing

- **Must hold:** every `engine.answer()` call writes `runs`, `run_evidence` and `run_steps`, a stopped run included, before the answer is returned. The `runs` row is written when the run starts and completed when it ends, and each step names its loop. The `Tracer` is the only writer. One masked trace per call goes to Langfuse, tagged with the entry point, tickers and environment.
- **Rules:** §12 in full; §7 (record tables).
- **Test:** end-to-end. A stopped run has its `runs` row, and a run killed midway leaves one that never reached `finished`. Each cited source has a `run_evidence` row whose hash matches. With Langfuse unreachable, the run completes and its record is stored. A planted key, and a payload from a masked source, never appear in an exported span.
- **Rests on:** check 3 (passed) and check 11.

### 1.8 Behaviour baseline

- **Must hold:** a hand-labelled question set exists: question, as-of date, expected chunk, expected answer, nothing from TipRanks. Some cases carry no expected answer and the stop reason they must end in, here `insufficient_evidence`. An evaluation run scores each case: retrieval hit, hard checks, evidence, path, response time, tokens, cost, and DeepEval's judged scores. The run accepted at a gate becomes the case's baseline. A later run produces a shift report against it, and the scores are mirrored to Langfuse.
- **Rules:** §11 ("Retrieval and answer quality", "Questions that must not be answered", "Behaviour shift"); §12.1 (`case_id`, the baseline, `run_scores`).
- **Test:** unit, on stored runs. A changed evidence set, a changed path, a slower run and a lower judged score are each reported as a shift. A case that was answered when it had to stop fails its hard check, and so does one that stopped when it had an answer; both rates are reported. A drop within the tolerance is not. An intended shift, once accepted, becomes the baseline.
- **Evaluation run at its gate:** yes. It sets the first baseline, on the thin text path.
- **Rests on:** check 6 (passed).

</details>

<details>
<summary><b>B. Real documents</b> (slices 1.9 to 1.15)</summary>

Each slice in this group changes the evidence a model reads, so each has an evaluation run at its gate.

### 1.9 Parse a born-digital PDF

- **Must hold:** the `models` service gains `/parse`. The `DocumentParser` interface returns typed elements with page, bounding box, caption and picture class. The service returns the Docling JSON without images, the Markdown and every crop; the `ArtifactStore` writes them, names each crop by its hash, never overwrites one, and returns a path relative to its root.
- **Rules:** §3 ("Parse into typed elements", "Crop, don't send the page", "Binary artifacts live on disk"); §9.1.
- **Test:** end-to-end on the synthetic fixture with the fake parser. Every element kind reaches the store with its crop reference. No graph state and no record holds image bytes. Ingesting the same file twice leaves its crops untouched. A GPU test runs the real `/parse` on the fixture, inside the container, and finds the tables and headings the fixture's manifest lists: Docling degrades without a word when the image has no fonts (§9.1).
- **Rests on:** check 4 (passed) and check 7a (passed).

### 1.10 Text chunks under their heading path

- **Must hold:** a text chunk is a heading plus the text under it, up to `chunking.max_chars`. A longer section is cut at paragraph boundaries, and every piece repeats its heading. Heading levels are inferred in code and stored as a path in `section`, and the embedded text starts with that path. A deck is chunked per slide. A filing chunk carries its Item in `filing_item`. A footnote is attached to the chunk that carries its marker. Page furniture is dropped. Text is NFKC-normalised and unmapped characters are repaired or counted. `ingest_runs` records the sizes of the chunks by kind, and how many were cut at the limit.
- **Rules:** §3 ("Text chunks follow headings", "A long section is cut at paragraphs", "A deck is chunked per slide", "A filing chunk knows its Item", "A footnote stays with what it qualifies", "Furniture is not content", "Text is normalised and its damage is counted"); §12.2 (chunk sizes).
- **Test:** unit for each rule. End-to-end on the fixture: a card under a column heading keeps that heading in its path; a long section gives pieces that each carry the path; a footnote sits with its paragraph; no header or footer line is a chunk; the counts and the chunk sizes in `ingest_runs` match the stored chunks.

### 1.11 Tables

- **Must hold:** a table Docling returns is checked in code, then stored as Markdown, or as label-value lines when it is a key-value grid. Prose mistaken for a table is demoted to text. Group rows are joined to their labels. A dash is a missing value. A value in parentheses is negative. Unit suffixes are read per cell, and a scale stated in the header reaches every cell without one. A subtotal row is flagged, and the detail rows must foot to it. A footnote marker is taken off the value and kept as a reference. Each change is counted. Verification against XBRL and a vision model is Phase 2.
- **Rules:** §3 ("A table must look like one", "Each element gets the representation it needs").
- **Test:** unit for each check. End-to-end on the fixture: the cases listed under "Layout regression" in §11 that concern tables.

### 1.12 Pictures and text printed in charts

- **Must hold:** a picture Docling classes as a logo or an icon, or one under the size limit, is dropped and counted. A captioned picture is kept as a `docling_picture` chunk with its crop and its context. Text printed in a chart is stored as a text chunk linked to the picture and is quotable; axis ticks are metadata. No vision model is called yet: chart classification, series and captions are Phase 3.
- **Rules:** §3 ("Decorative pictures never reach the vision model", "A crop travels with its context", "Text printed in a chart is text").
- **Test:** unit for the filter. End-to-end on the fixture: a logo on every page and a badge produce no chunk; a printed data label is a quotable chunk whose parent is the chart.

### 1.13 OCR routing and OCR

- **Must hold:** a page goes to OCR when its text layer is thin and a raster image covers most of it. The count is taken on the PDF's own text layer, and every page is counted, empty ones included. The picture boxes are masked before OCR, which runs through the `/ocr` endpoint and the `OcrEngine` interface of slice 0.8. OCR text becomes `lighton_ocr` chunks; what the OCR model writes for a picture's crop becomes an `ocr_picture_text` chunk linked to the picture, at a lower confidence, never quotable.
- **Rules:** §3 ("OCR routing is a measurement", "A model's reading of a picture is never text").
- **Test:** unit for the routing decision, which is a control. End-to-end with the fake OCR engine: every page of the image-only fixture is routed; a born-digital page holding only a chart and a table is not; the page text holds nothing from inside a picture's box. A GPU test measures real OCR accuracy on the rasterised filing pages against their text layer.
- **Rests on:** slice 0.8, check 7a (passed) and check 9.

### 1.14 Acquisition without an agent

- **Must hold:** `engine.refresh()` resolves the configured sources of one equity and one ETF from `config/sources.yaml`, fetches their documents through the `HttpFetcher` interface, drops repeats by content hash, and ingests the rest. A URL outside the permitted domains is refused. `engine.ingest(path, ticker, source)` ingests a supplied file with its source, such as an analyst report PDF.
- **Rules:** §2 ("Where sources live", "Only permitted sources are fetched", the download and supplied-file paths); §4 (the source's truth coefficient is resolved from config, never stored on a chunk).
- **Test:** end-to-end with the fake fetcher. The same document fetched twice is stored once. A supplied file carries its declared source. A fetch from a domain that is not permitted raises `ToolDenied`. A `network` test fetches one real filing and one real fund document.
- **Rests on:** check 8.

### 1.15 A refresh never destroys

- **Must hold:** ingesting a document whose content changed stores a new version with its own chunks; the earlier version is marked `superseded_at` and is excluded from retrieval, unless the as-of date falls before that moment. Unchanged content writes nothing.
- **Rules:** §7 ("A refresh adds; it never destroys"); §4 ("Known by then, or not admissible").
- **Test:** end-to-end. After a changed re-ingest, a question as of today sees only the new version and a question as of an earlier date sees the old one. Replaying a stored run reports which of its sources changed.

</details>

<details>
<summary><b>C. Better ranking</b> (slice 1.16)</summary>

### 1.16 Normalisation, reranking and source truth

- **Must hold:** the pool is the union of the two candidate lists of slice 1.3. Vector scores are standardised within text chunks and within visual chunks before merging. The `models` service gains `/rerank`, and the `Reranker` scores one candidate at a time, a crop for a visual chunk and text for prose, falling back to text and then to retrieval order; with the reranker off, the two lists are merged by reciprocal rank fusion. Each score is then weighed by the source's truth coefficient. `policy.min_source_truth` excludes weaker sources as a predicate.
- **Rules:** §4 ("Modality normalisation", "Source truth weighs the order", "Source truth can be a floor").
- **Test:** end-to-end with the fake reranker. With normalisation on, the best table rises into the top results where it did not before. A filing passage outranks a third-party passage of equal relevance, and a weight of 0 restores the order by relevance. With the floor set, the weaker source is not retrieved.
- **Evaluation run at its gate:** yes. This is the slice the baseline of 1.8 exists to measure.
- **Rests on:** the rest of check 7 (the reranker fits on the GPU beside the other models). Check 5 measured the gap this slice corrects: a question scored 0.67 against a matching sentence and 0.49 against the matching image.

</details>

## Closing the phase

- Every slice has passed its gate.
- An evaluation run on the real models shows no unintended shift against the baseline: retrieval hit rate, judged scores, response time and cost per question, and time and cost per page for the whole fixture corpus.
- The two sample documents have been ingested with the real models (§11 "Sample documents"): the analyst report PDF through Docling, and the rasterised filing pages through LightOnOCR with their measured accuracy.
- `01_ingestion` and `02_retrieval` run from top to bottom.

## Left to later phases

- **Phase 2:** typed records with their basis and scope, the reason a figure is missing, prices, table verification, news, and analyst views from an optional provider, fetched on demand.
- **Phase 3:** chart classification, series and captions; the provenance gate, with its number check.
- **Phase 4:** generated questions and trajectory ranking.
- **Phase 5:** the agents with their instructions files, the skills and their typed scripts, the stop on a question outside the universe or too unclear to plan, the safety gates, the no-progress stop, sessions and long-term memory.
