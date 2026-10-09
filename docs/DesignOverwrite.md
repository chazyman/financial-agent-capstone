# Design overwrites

Each entry records a change to `PROPOSAL.md` or `DESIGN.md` that conflicted with a design invariant and that the user approved. Newest at the bottom.

<!-- Entry format:

## YYYY-MM-DD · short title
- Invariant before: the line as it was
- Invariant now: the line as it is
- Changed: the DESIGN / PROPOSAL sections

-->

## 2026-10-09 · Self-learning is no longer listed as out of scope
- Invariant before: **Out of scope:** portfolio management, time-series forecasting, audio, self-learning, productionization into an agent harness (`PROPOSAL.md` §1).
- Invariant now: **Out of scope:** portfolio management, time-series forecasting, audio, productionization into an agent harness (`PROPOSAL.md` §1).
- Changed: `PROPOSAL.md` §4.5 and `docs/DESIGN.md` §12.1 no longer say self-learning is not in scope. It stays a later extension the run record is kept ready for (`docs/DESIGN.md` §9, "Built to be extended", and §12.1).
