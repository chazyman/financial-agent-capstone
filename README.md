# Financial AI Agent Assistant

Capstone for the *Agentic AI for Finance* course. A multimodal RAG pipeline and agent that answers analyst-style questions over filings, fund documents, CSVs, news and analyst ratings, with every figure traceable to its source.

**Status:** design phase. Phase 0 will scaffold the Python AI engine and the Jupyter notebooks that wrap it.

## Documents

| File | What it is |
|---|---|
| [PROPOSAL.md](PROPOSAL.md) | What and why, for course reviewers |
| [docs/DESIGN.md](docs/DESIGN.md) | Implementation detail: tools, skills, data model, structure, deployment, config |
| [docs/slices/](docs/slices/) | The slices of each phase, in order: what must hold and how it is tested |
| [docs/DesignOverwrite.md](docs/DesignOverwrite.md) | Each approved change that overturned an invariant, or removed or reinstated a rule |
| [docs/ArchitecturalReview.md](docs/ArchitecturalReview.md) | The review held before the first coding session, one decision per topic |

## Review log

Reviews of the project's design and process, newest at the bottom.

| Date | Issue | Decision | Record |
|---|---|---|---|
| 2026-10-09 | Design document bloat and process review: `docs/DESIGN.md` had grown to about 28,000 words before any code existed | The text for a phase that is not yet sliced is a draft. When work on the phase starts, it is corrected once and compacted: each rule and its reason stay, and the detail moves to the phase's slice file | [Architectural Review](docs/ArchitecturalReview.md), topic 1; [DESIGN §9.3](docs/DESIGN.md#93-phases-and-their-slices) |
| 2026-10-09 | Architectural review before the first coding session: are the choices that Phase 0 and Phase 1 code will lock in the right ones? | All five lock-ins are kept, and `Engine` gains a lifecycle for its resources. A document, a chunk and a news item can concern zero, one or several instruments. Market and economic news will be stored and shown as context, with no modelling of impact; that part waits for Phase 2 | [Architectural Review](docs/ArchitecturalReview.md), topics 2 to 7; [slices](docs/slices/) |

## Getting started

Build, test and run commands will be in [DESIGN §9.2](docs/DESIGN.md#92-development) once Phase 0 lands.

## Credits

Built on Nicole Königstein's *Agentic AI for Finance* course and [*AI Agents: The Definitive Guide*](https://github.com/Nicolepcx/ai-agents-the-definitive-guide).
