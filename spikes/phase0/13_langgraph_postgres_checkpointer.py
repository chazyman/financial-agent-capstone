# /// script
# requires-python = ">=3.12"
# dependencies = [
#   "langgraph==1.2.12",
#   "langgraph-checkpoint==4.2.0",
#   "langgraph-checkpoint-postgres==3.1.2",
#   "psycopg[binary]==3.3.6",
#   "sqlalchemy[asyncio]==2.1.4",
#   "alembic==1.20.0",
# ]
# ///
"""Check 13a, 13b, 13d and 13e: LangGraph's Postgres checkpointer beside our own stores.

Assumption (DESIGN section 13.5): LangGraph's Postgres checkpointer works on psycopg 3,
in its own schema, beside our stores and untouched by Alembic; a graph with a sub-graph
and a run context resumes after its process is killed; and a node can be timed out.

Setup (a throwaway database, no `.env`, no metered call). Run from spikes/phase0:

    docker network create spike13
    docker run -d --rm --name spike13-pg --network spike13 \
      -e POSTGRES_PASSWORD=spike pgvector/pgvector:pg17
    docker run --rm --network spike13 \
      --user "$(id -u):$(id -g)" -e HOME=/tmp -e UV_CACHE_DIR=/cache/uv \
      -v "$HOME/.cache/uv":/cache/uv -v "$PWD":/spike:ro \
      ghcr.io/astral-sh/uv:python3.12-bookworm-slim \
      uv run /spike/13_langgraph_postgres_checkpointer.py \
      postgresql://postgres:spike@spike13-pg:5432/postgres
    docker stop spike13-pg; docker network rm spike13

PASS CRITERION (approved by the user before this script was run)

13a, the checkpointer beside our stores
  - AsyncPostgresSaver.setup() runs on psycopg 3 and creates its tables in the schema
    `memory`; no table of the checkpointer is in `public`.
  - A table of our own, created through async SQLAlchemy on the same database, works
    beside them (insert and select).
  - An Alembic-style upgrade and downgrade of our table (create, drop, create) leaves the
    checkpoint tables and their row counts intact; Alembic's autogenerate comparison,
    with a schema filter that skips `memory`, proposes nothing that touches it.

13b, a killed run resumes
  - A graph a -> sub(s1 -> s2) -> c -> d, run with a run context (run id, as-of date),
    is killed by SIGKILL in a child process. Two kills: at the start of the parent step
    `c` (between two parent steps) and at the start of `s2` (inside the sub-graph).
  - A new process resumes it from the checkpoint (`ainvoke(None, ...)` on the same thread).
  - No completed step runs again, counted from a side-effect log each node writes: a step
    that ended before the kill has exactly one `end`; the step that was cut short has two
    `start` and one `end`.
  - The final state is complete and exact: the spend counter is 5 and the trail is
    [a, s1, s2, c, d], so nothing was lost and nothing was counted twice. Every node saw
    the same as-of date, on the first run and on the resume.
  - A third scenario, C, visits the sub-graph node twice (the plan walker's loop): the
    graph goes a -> sub -> c -> sub -> c -> d, and the kill is at the start of `s2` in the
    second visit. After the resume each node has the counts below: `s1` two starts and two
    ends (one pair per visit, so the first visit was not rerun and the second visit's `s1`
    was not rerun), `s2` three starts and two ends, `c` two and two; the final spend is 8
    and the trail [a, s1, s2, c, s1, s2, c, d].
  - The sub-graph keeps its own state schema and is called from a node, which returns only
    the sub-graph's delta (its spend and its trail). Saves are made with durability="sync",
    so the checkpoint of a step is written before the next one starts. What the default
    does is not under test here.

13e, two levels of nesting (supervisor, agent sub-graph, the retrieval sub-graph through a tool)
  - Graph: a -> agent(think -> tool(i1 -> i2)) -> c. `agent` is a sub-graph called from a
    parent node; `tool` is a node of that sub-graph which calls an inner graph (i1 -> i2)
    with ainvoke, as the `retrieve` tool would; each level keeps its own state and returns
    its delta.
  - Two kills: at the start of `tool` (between the levels) and at the start of `i2` (inside
    the inner graph).
  - After the resume, every node that ended before the kill has one `end` and no second
    `start`; the cut-short node has two `start` and one `end` (for the kill inside `i2`,
    `tool` is also cut short: two `start`, one `end`, and `i1` is not rerun).
  - The final spend is 6 and the trail [a, think, i1, i2, tool, c]: each level counts its
    spend once. The same as-of date reaches every level.
  - Fallback if it fails: run the retrieval graph inside a tool without checkpoints of its
    own, so a cut-short `retrieve` call reruns whole (DESIGN 13.1: a cut-short step reruns).

13d, a node timeout
  - PASS if LangGraph 1.2.12 has a timeout on a node itself that stops a slow async node
    within the limit. If it has none, the script records that, whether a step-level
    timeout exists, and whether `asyncio.timeout` in a wrapper stops the node; 13d then
    reads FAIL, and the fallback of the README row (a wrapper) is what is written.

The script prints `RESULT: PASS` only if 13a, 13b, 13d and 13e all pass, and the versions
used. Fallback if 13a or 13b fails: the run record's `run_steps` as the resume log.

RECORD OF RUNS
  - First run (2026-10-12): 13a PASS, 13d PASS, 13b FAIL. The fault was in the test graph: the
    sub-graph shared the parent's state keys with additive reducers, so the parent added the
    sub-graph's whole state to its own (spent 6, trail [a, a, s1, s2, c, d]). The node counts
    showed no step repeated. Scenarios C and 13e were added, with the user's approval, and
    the sub-graph now keeps its own state.
  - Second run (2026-10-12): stopped before 13b and 13e ran their scenarios, on a file name built
    from a label that held a "/". A fault of the script, not a result. 13a and 13d passed again.
  - Third run (2026-10-12): all of 13a, 13b (three scenarios), 13d and 13e PASS.
  - Fourth run (2026-10-12): the same, after the dependencies were pinned to the versions of the
    third run; every node count and final state was unchanged. RESULT: PASS.
"""

import asyncio
import inspect
import json
import operator
import os
import signal
import subprocess
import sys
import tempfile
import time
import traceback
from dataclasses import dataclass
from importlib.metadata import version
from typing import Annotated, TypedDict

import psycopg
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.autogenerate import compare_metadata
from alembic.operations import Operations
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.graph import END, START, StateGraph
from langgraph.runtime import Runtime
from psycopg import AsyncConnection
from psycopg.rows import dict_row
from sqlalchemy.ext.asyncio import create_async_engine

AS_OF = "2026-03-31"
SCHEMA = "memory"
EXPECTED_TABLES = {"checkpoints", "checkpoint_blobs", "checkpoint_writes", "checkpoint_migrations"}
LOG = ""  # path of the side-effect log; set in the child process and in the parent


class State(TypedDict):
    spent: Annotated[int, operator.add]
    trail: Annotated[list[str], operator.add]


class Private(TypedDict):
    """State of a sub-graph: its own schema, never shared with its caller."""

    spent: Annotated[int, operator.add]
    trail: Annotated[list[str], operator.add]


@dataclass(frozen=True)
class Ctx:
    run_id: str
    as_of: str


def note(event: str, node: str, as_of: str) -> None:
    with open(LOG, "a") as f:
        f.write(f"{event}|{node}|{as_of}\n")


def should_kill(name: str) -> bool:
    """KILL_AT is `name` (the first start) or `name#n` (the n-th start of that node)."""
    spec = os.environ.get("KILL_AT", "")
    if not spec:
        return False
    target, _, nth = spec.partition("#")
    if target != name:
        return False
    if not nth:
        return True
    starts = sum(1 for e, n, _ in read_log(LOG) if e == "start" and n == name)
    return starts == int(nth)


async def run_private(graph, ctx: Ctx) -> dict:
    """Call a sub-graph from a node with a fresh private state; return only its delta."""
    out = await graph.ainvoke({"spent": 0, "trail": []}, context=ctx, durability="sync")
    return {"spent": out["spent"], "trail": out["trail"]}


def make_node(name: str, inner=None):
    async def node(state, runtime: Runtime[Ctx]):
        ctx = runtime.context
        note("start", name, ctx.as_of)
        if should_kill(name):
            note("kill", name, ctx.as_of)
            os.kill(os.getpid(), signal.SIGKILL)
        await asyncio.sleep(0.05)
        delta = {"spent": 1, "trail": [name]}
        if inner is not None:  # a tool node that runs a graph, as `retrieve` would
            d = await run_private(inner, ctx)
            delta = {"spent": 1 + d["spent"], "trail": d["trail"] + [name]}
        note("end", name, ctx.as_of)
        return delta

    node.__name__ = name
    return node


def delegate(graph):
    """A parent node that calls a sub-graph and passes up its delta; it has no step of its own."""

    async def node(state, runtime: Runtime[Ctx]):
        return await run_private(graph, runtime.context)

    return node


def private_graph(names: list[str], inners: dict | None = None):
    g = StateGraph(Private, context_schema=Ctx)
    inners = inners or {}
    for n in names:
        g.add_node(n, make_node(n, inners.get(n)))
    g.add_edge(START, names[0])
    for x, y in zip(names, names[1:]):
        g.add_edge(x, y)
    g.add_edge(names[-1], END)
    return g.compile()  # no checkpointer of its own: it inherits the caller's


def build_graph(saver, kind: str = "plain"):
    parent = StateGraph(State, context_schema=Ctx)
    parent.add_node("a", make_node("a"))
    if kind in ("plain", "revisit"):
        parent.add_node("sub", delegate(private_graph(["s1", "s2"])))
        parent.add_node("c", make_node("c"))
        parent.add_node("d", make_node("d"))
        parent.add_edge(START, "a")
        parent.add_edge("a", "sub")
        parent.add_edge("sub", "c")
        if kind == "revisit":
            parent.add_conditional_edges(
                "c", lambda st: "sub" if st["trail"].count("c") < 2 else "d", {"sub": "sub", "d": "d"})
        else:
            parent.add_edge("c", "d")
        parent.add_edge("d", END)
    elif kind == "nested":
        inner = private_graph(["i1", "i2"])
        agent = private_graph(["think", "tool"], inners={"tool": inner})
        parent.add_node("agent", delegate(agent))
        parent.add_node("c", make_node("c"))
        parent.add_edge(START, "a")
        parent.add_edge("a", "agent")
        parent.add_edge("agent", "c")
        parent.add_edge("c", END)
    else:
        raise ValueError(kind)
    return parent.compile(checkpointer=saver)


async def open_saver(dsn: str):
    conn = await AsyncConnection.connect(
        dsn,
        autocommit=True,
        prepare_threshold=0,
        row_factory=dict_row,
        options=f"-c search_path={SCHEMA}",
    )
    return AsyncPostgresSaver(conn), conn


async def child(dsn: str, thread: str, logfile: str, mode: str, kind: str) -> None:
    global LOG
    LOG = logfile
    saver, conn = await open_saver(dsn)
    graph = build_graph(saver, kind)
    cfg = {"configurable": {"thread_id": thread}}
    ctx = Ctx(run_id=thread, as_of=AS_OF)
    inp = {"spent": 0, "trail": []} if mode == "run" else None
    out = await graph.ainvoke(inp, cfg, context=ctx, durability="sync")
    print("FINAL " + json.dumps(out))
    await conn.close()


def spawn(dsn: str, thread: str, logfile: str, mode: str, kind: str, kill_at: str = "") -> subprocess.CompletedProcess:
    env = {**os.environ, "KILL_AT": kill_at}
    return subprocess.run(
        [sys.executable, os.path.abspath(__file__), "child", dsn, thread, logfile, mode, kind],
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )


def read_log(logfile: str) -> list[tuple[str, str, str]]:
    with open(logfile) as f:
        return [tuple(line.strip().split("|")) for line in f if line.strip()]


def wait_for_postgres(dsn: str) -> None:
    for _ in range(60):
        try:
            with psycopg.connect(dsn, connect_timeout=2):
                return
        except psycopg.OperationalError:
            time.sleep(1)
    raise RuntimeError("PostgreSQL did not come up in 60 s")


def admin(dsn: str) -> psycopg.Connection:
    return psycopg.connect(dsn, autocommit=True)


def row_counts(dsn: str) -> dict[str, int]:
    with admin(dsn) as c:
        return {t: c.execute(f"SELECT count(*) FROM {SCHEMA}.{t}").fetchone()[0] for t in sorted(EXPECTED_TABLES)}


# ---------------------------------------------------------------- 13a

async def check_13a(dsn: str) -> bool:
    global LOG
    ok = True
    sa_url = dsn.replace("postgresql://", "postgresql+psycopg://", 1)

    with admin(dsn) as c:
        c.execute(f"CREATE SCHEMA IF NOT EXISTS {SCHEMA}")

    saver, conn = await open_saver(dsn)
    await saver.setup()
    # one real run, so the tables hold rows
    LOG = tempfile.mkstemp(prefix="log13a_")[1]
    graph = build_graph(saver, "plain")
    await graph.ainvoke(
        {"spent": 0, "trail": []},
        {"configurable": {"thread_id": "t13a"}},
        context=Ctx(run_id="t13a", as_of=AS_OF),
        durability="sync",
    )
    await conn.close()

    with admin(dsn) as c:
        in_memory = {r[0] for r in c.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = %s", (SCHEMA,))}
        in_public = {r[0] for r in c.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'")}
    leaked = EXPECTED_TABLES & in_public
    print(f"13a  tables in schema {SCHEMA}: {sorted(in_memory)}")
    print(f"13a  tables in public: {sorted(in_public)}")
    if not EXPECTED_TABLES <= in_memory or leaked:
        print(f"13a  FAIL: expected {sorted(EXPECTED_TABLES)} in {SCHEMA}, none in public; leaked {sorted(leaked)}")
        ok = False
    before = row_counts(dsn)
    print(f"13a  row counts after one run: {before}")
    if before["checkpoints"] < 1:
        print("13a  FAIL: the run wrote no checkpoint")
        ok = False

    # our own table, async SQLAlchemy on psycopg 3
    engine = create_async_engine(sa_url)
    async with engine.begin() as c:
        await c.execute(sa.text("CREATE TABLE our_items (id integer primary key, name text)"))
        await c.execute(sa.text("INSERT INTO our_items VALUES (1, 'x')"))
    async with engine.connect() as c:
        got = (await c.execute(sa.text("SELECT name FROM our_items WHERE id = 1"))).scalar_one()
    print(f"13a  our own table through async SQLAlchemy (driver {engine.dialect.driver}): read back {got!r}")
    if got != "x":
        ok = False

    # Alembic: upgrade / downgrade of our table, autogenerate comparison with a filter
    metadata = sa.MetaData()
    sa.Table("our_items", metadata, sa.Column("id", sa.Integer, primary_key=True), sa.Column("name", sa.Text))

    def alembic_steps(sync_conn):
        ops = Operations(MigrationContext.configure(sync_conn))
        ops.drop_table("our_items")  # downgrade
        ops.create_table("our_items", sa.Column("id", sa.Integer, primary_key=True), sa.Column("name", sa.Text))  # upgrade

    def include_name(name, type_, parent_names):
        return False if type_ == "schema" and name == SCHEMA else True

    def diff(sync_conn, filtered: bool):
        opts = {"include_schemas": True}
        if filtered:
            opts["include_name"] = include_name
        return compare_metadata(MigrationContext.configure(sync_conn, opts=opts), metadata)

    async with engine.begin() as c:
        await c.run_sync(alembic_steps)
    async with engine.connect() as c:
        filtered = await c.run_sync(lambda s: diff(s, True))
        unfiltered = await c.run_sync(lambda s: diff(s, False))
    await engine.dispose()
    after = row_counts(dsn)
    print(f"13a  row counts after the Alembic steps: {after}")
    print(f"13a  autogenerate diff with the schema filter: {filtered!r}")
    print(f"13a  autogenerate diff without it touches {SCHEMA}: {SCHEMA in repr(unfiltered)}")
    if after != before:
        print("13a  FAIL: the checkpoint tables changed")
        ok = False
    if filtered:
        print("13a  FAIL: autogenerate proposes changes with the filter on")
        ok = False
    print(f"13a  {'PASS' if ok else 'FAIL'}")
    return ok


# ---------------------------------------------------------------- 13b

async def pre_resume_state(dsn: str, thread: str, kind: str) -> dict:
    saver, conn = await open_saver(dsn)
    snap = await build_graph(saver, kind).aget_state({"configurable": {"thread_id": thread}})
    await conn.close()
    return dict(snap.values)


async def scenario(dsn: str, label: str, kind: str, kill_at: str,
                   counts: dict[str, tuple[int, int]], spent: int, trail: list[str]) -> bool:
    """counts: node -> (starts, ends) over the whole log, kill and resume together."""
    global LOG
    ok = True
    slug = label.replace("/", "_")
    thread = f"t-{slug}"
    logfile = tempfile.mkstemp(prefix=f"log_{slug}_")[1]
    LOG = logfile

    first = spawn(dsn, thread, logfile, "run", kind, kill_at=kill_at)
    killed = first.returncode == -signal.SIGKILL
    print(f"{label}  first process exit code {first.returncode} (killed by SIGKILL: {killed})")
    if not killed:
        print(first.stderr[-1500:])
        return False
    pre = await pre_resume_state(dsn, thread, kind)
    print(f"{label}  parent state at the kill, read by a third process: {pre}")

    second = spawn(dsn, thread, logfile, "resume", kind)
    if second.returncode != 0:
        print(f"{label}  FAIL: the resume exited {second.returncode}\n{second.stderr[-2000:]}")
        return False
    final = json.loads(second.stdout.strip().splitlines()[-1].removeprefix("FINAL "))
    print(f"{label}  final state: {final}")

    log = read_log(logfile)
    for node, want in counts.items():
        got = (sum(1 for e, n, _ in log if e == "start" and n == node),
               sum(1 for e, n, _ in log if e == "end" and n == node))
        flag = "" if got == want else f"  <-- expected {want[0]} start, {want[1]} end"
        print(f"{label}  {node}: {got[0]} start, {got[1]} end{flag}")
        ok = ok and not flag
    unlisted = {n for _, n, _ in log} - set(counts)
    if unlisted:
        print(f"{label}  FAIL: nodes outside the expected set ran: {sorted(unlisted)}")
        ok = False
    if final.get("spent") != spent or final.get("trail") != trail:
        print(f"{label}  FAIL: final state is not spent={spent}, trail={trail}")
        ok = False
    dates = {d for _, _, d in log}
    print(f"{label}  as-of dates seen by the nodes: {sorted(dates)}")
    ok = ok and dates == {AS_OF}
    print(f"{label}  {'PASS' if ok else 'FAIL'}")
    return ok


ONCE = (1, 1)
PLAIN_TRAIL = ["a", "s1", "s2", "c", "d"]
NESTED_TRAIL = ["a", "think", "i1", "i2", "tool", "c"]


async def check_13b(dsn: str) -> bool:
    a = await scenario(dsn, "13b/between-parent-steps", "plain", "c",
                       {"a": ONCE, "s1": ONCE, "s2": ONCE, "c": (2, 1), "d": ONCE}, 5, PLAIN_TRAIL)
    b = await scenario(dsn, "13b/inside-sub-graph", "plain", "s2",
                       {"a": ONCE, "s1": ONCE, "s2": (2, 1), "c": ONCE, "d": ONCE}, 5, PLAIN_TRAIL)
    c = await scenario(dsn, "13b/second-visit", "revisit", "s2#2",
                       {"a": ONCE, "s1": (2, 2), "s2": (3, 2), "c": (2, 2), "d": ONCE}, 8,
                       ["a", "s1", "s2", "c", "s1", "s2", "c", "d"])
    return a and b and c


async def check_13e(dsn: str) -> bool:
    a = await scenario(dsn, "13e/between-levels", "nested", "tool",
                       {"a": ONCE, "think": ONCE, "tool": (2, 1), "i1": ONCE, "i2": ONCE, "c": ONCE},
                       6, NESTED_TRAIL)
    b = await scenario(dsn, "13e/inside-inner-graph", "nested", "i2",
                       {"a": ONCE, "think": ONCE, "tool": (2, 1), "i1": ONCE, "i2": (2, 1), "c": ONCE},
                       6, NESTED_TRAIL)
    return a and b


# ---------------------------------------------------------------- 13d

class Slow(TypedDict):
    n: int


async def slow_node(state: Slow):
    await asyncio.sleep(3)
    return {"n": state["n"] + 1}


def slow_graph(**add_node_kwargs):
    g = StateGraph(Slow)
    g.add_node("slow", slow_node, **add_node_kwargs)
    g.add_edge(START, "slow")
    g.add_edge("slow", END)
    return g


async def timed(graph, limit: float = 0.3) -> tuple[str, float]:
    t0 = time.monotonic()
    try:
        await graph.ainvoke({"n": 0})
        return "completed", time.monotonic() - t0
    except BaseException as e:  # noqa: BLE001
        return f"raised {type(e).__name__}", time.monotonic() - t0


async def check_13d() -> bool:
    import langgraph.types as lt

    params = list(inspect.signature(StateGraph.add_node).parameters)
    timeout_params = [p for p in params if "timeout" in p.lower()]
    type_names = [n for n in dir(lt) if "imeout" in n]
    print(f"13d  add_node parameters: {params}")
    print(f"13d  timeout-related add_node parameters: {timeout_params}; names in langgraph.types: {type_names}")

    native = False
    for p in timeout_params:
        candidates = [0.3]
        for tn in type_names:
            cls = getattr(lt, tn)
            for kw in ({}, {"run_timeout": 0.3}, {"timeout": 0.3}):
                try:
                    candidates.append(cls(0.3) if not kw else cls(**kw))
                except Exception:  # noqa: BLE001
                    pass
        for cand in candidates:
            try:
                graph = slow_graph(**{p: cand}).compile()
            except Exception as e:  # noqa: BLE001
                print(f"13d  add_node({p}={cand!r}) refused: {type(e).__name__}: {e}")
                continue
            outcome, secs = await timed(graph)
            print(f"13d  native {p}={cand!r}: {outcome} after {secs:.2f} s")
            if outcome != "completed" and secs < 1.5:
                native = True
                break
        if native:
            break

    # step-level timeout, for the record
    g = slow_graph().compile()
    if hasattr(g, "step_timeout"):
        g.step_timeout = 0.3
        outcome, secs = await timed(g)
        print(f"13d  step_timeout=0.3: {outcome} after {secs:.2f} s (a step, not one node)")
    else:
        print("13d  no step_timeout attribute")

    # the fallback: asyncio.timeout in a wrapper
    async def wrapped(state: Slow):
        try:
            async with asyncio.timeout(0.3):
                return await slow_node(state)
        except TimeoutError:
            return {"n": -1}

    g = StateGraph(Slow)
    g.add_node("slow", wrapped)
    g.add_edge(START, "slow")
    g.add_edge("slow", END)
    t0 = time.monotonic()
    out = await g.compile().ainvoke({"n": 0})
    secs = time.monotonic() - t0
    wrapper_ok = out == {"n": -1} and secs < 1.5
    print(f"13d  asyncio.timeout in a wrapper: state {out} after {secs:.2f} s -> {'works' if wrapper_ok else 'does not work'}")

    print(f"13d  native node timeout: {'yes' if native else 'no'}; wrapper fallback: {'works' if wrapper_ok else 'fails'}")
    print(f"13d  {'PASS' if native else 'FAIL'}")
    return native


# ---------------------------------------------------------------- main

async def main(dsn: str) -> None:
    global LOG
    print("versions:", {p: version(p) for p in [
        "langgraph", "langgraph-checkpoint", "langgraph-checkpoint-postgres", "psycopg", "sqlalchemy", "alembic"]})
    wait_for_postgres(dsn)
    results = {}
    for name, fn in [("13a", lambda: check_13a(dsn)), ("13b", lambda: check_13b(dsn)),
                     ("13d", check_13d), ("13e", lambda: check_13e(dsn))]:
        try:
            results[name] = await fn()
        except Exception:  # noqa: BLE001
            traceback.print_exc()
            results[name] = False
    print("summary:", {k: ("PASS" if v else "FAIL") for k, v in results.items()})
    print("RESULT: PASS" if all(results.values()) else "RESULT: FAIL")


if __name__ == "__main__":
    if sys.argv[1] == "child":
        asyncio.run(child(*sys.argv[2:7]))
    else:
        asyncio.run(main(sys.argv[1]))
