# /// script
# requires-python = ">=3.12"
# dependencies = ["langgraph"]
# ///
"""Phase 0 check 1: does LangGraph run a sync node in a thread when the graph is run with ainvoke?

Assumption (DESIGN §8): the engine's entry points are async, and a node may be sync or async.
Pass criterion, written before running:
  a. a sync node that blocks for 1 s runs off the event-loop thread;
  b. two concurrent runs of that graph finish in under 1.6 s (they overlap);
  c. the event loop keeps ticking while the sync node blocks;
  d. each run sees its own run context (context_schema), in sync and in async nodes.
Also measured, for information: what happens when an async node calls blocking code directly.
Run: uv run 01_langgraph_sync_node_under_async.py
"""
import asyncio
import importlib.metadata as md
import threading
import time
from dataclasses import dataclass
from typing import TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.runtime import Runtime


class State(TypedDict, total=False):
    sync_thread: str
    async_thread: str
    as_of_sync: str
    as_of_async: str


@dataclass(frozen=True)
class RunContext:
    as_of: str


def sync_node(state: State, runtime: Runtime[RunContext]) -> State:
    time.sleep(1.0)  # stands in for a blocking database or HTTP call
    return {"sync_thread": threading.current_thread().name, "as_of_sync": runtime.context.as_of}


async def async_node(state: State, runtime: Runtime[RunContext]) -> State:
    await asyncio.sleep(0.01)
    return {"async_thread": threading.current_thread().name, "as_of_async": runtime.context.as_of}


async def async_node_calling_blocking_code(state: State, runtime: Runtime[RunContext]) -> State:
    time.sleep(1.0)  # a sync store or HTTP client called from an async tool
    return {"async_thread": threading.current_thread().name}


def build(first):
    g = StateGraph(State, context_schema=RunContext)
    g.add_node("first", first)
    g.add_node("second", async_node)
    g.add_edge(START, "first")
    g.add_edge("first", "second")
    g.add_edge("second", END)
    return g.compile()


async def timed_pair(graph):
    ticks = 0

    async def heartbeat():
        nonlocal ticks
        while True:
            await asyncio.sleep(0.05)
            ticks += 1

    hb = asyncio.create_task(heartbeat())
    t0 = time.perf_counter()
    r1, r2 = await asyncio.gather(
        graph.ainvoke({}, context=RunContext("2026-06-30")),
        graph.ainvoke({}, context=RunContext("2026-09-30")),
    )
    elapsed = time.perf_counter() - t0
    hb.cancel()
    return r1, r2, elapsed, ticks


async def main() -> None:
    print(f"langgraph {md.version('langgraph')}")
    main_thread = threading.current_thread().name

    r1, r2, elapsed, ticks = await timed_pair(build(sync_node))
    checks = {
        "a. sync node ran off the event-loop thread": r1["sync_thread"] != main_thread,
        "b. two runs overlapped (under 1.6 s)": elapsed < 1.6,
        "c. event loop kept ticking (15 or more ticks)": ticks >= 15,
        "d. own context in the sync node": (r1["as_of_sync"], r2["as_of_sync"]) == ("2026-06-30", "2026-09-30"),
        "d. own context in the async node": (r1["as_of_async"], r2["as_of_async"]) == ("2026-06-30", "2026-09-30"),
    }
    print(f"sync node thread: {r1['sync_thread']}; loop thread: {main_thread}; wall {elapsed:.2f} s; ticks {ticks}")
    for name, ok in checks.items():
        print(f"  {'PASS' if ok else 'FAIL'}  {name}")

    _, _, elapsed_b, ticks_b = await timed_pair(build(async_node_calling_blocking_code))
    print(f"for information, async node calling blocking code directly: wall {elapsed_b:.2f} s; ticks {ticks_b}")

    try:
        build(async_node).invoke({}, context=RunContext("x"))
        print("for information, sync invoke() of a graph with async nodes: worked")
    except Exception as exc:  # noqa: BLE001 - the check is whether it raises at all
        print(f"for information, sync invoke() of a graph with async nodes: {type(exc).__name__}")

    print("RESULT:", "PASS" if all(checks.values()) else "FAIL")


asyncio.run(main())
