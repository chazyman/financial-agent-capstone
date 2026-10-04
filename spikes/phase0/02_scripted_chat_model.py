# /// script
# requires-python = ">=3.12"
# dependencies = ["langchain-core", "pydantic"]
# ///
"""Phase 0 check 2: can a scripted fake chat model return tool calls and structured output?

Assumption (DESIGN §8, §9.2): tests run on a scripted chat model that a test hands its answers to,
and the run controls (budget counting, fallback) wrap any chat model, so a fake goes through them too.
Pass criterion, written before running: a fake of 40 lines or fewer
  a. returns a tool call through bind_tools;
  b. returns a validated Pydantic object through with_structured_output, sync and async;
  c. is counted by a callback handler (the budget) and can be given a fallback (model fallback).
Also recorded, for information: whether LangChain's stock fake does a and b.
Run: uv run 02_scripted_chat_model.py
"""
import asyncio
import importlib.metadata as md
from typing import Any

from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.tools import tool
from pydantic import BaseModel


class Claim(BaseModel):
    text: str
    source_kind: str
    source_id: str
    kind: str


class Draft(BaseModel):
    claims: list[Claim]


@tool
def record_lookup(metric: str, ticker: str) -> str:
    """Exact lookup of a typed record."""
    return "financial_facts:1234"


class ScriptedChatModel(BaseChatModel):
    """A chat model that answers with the messages a test scripted, in order."""

    script: list[Any]
    seen: list[Any] = []

    @property
    def _llm_type(self) -> str:
        return "scripted"

    def _generate(self, messages, stop=None, run_manager=None, **kwargs) -> ChatResult:
        if not self.script:
            raise RuntimeError("script exhausted")
        self.seen.append(messages)
        answer = self.script.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return ChatResult(generations=[ChatGeneration(message=answer)])

    def bind_tools(self, tools, **kwargs):
        return self


def tool_call(name: str, args: dict) -> AIMessage:
    return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": "call-1"}])


FORGED = {"claims": [{"text": "Revenue was $41.2M", "source_kind": "chunk", "source_id": "chart-7", "kind": "magnitude"}]}


class CallCounter(BaseCallbackHandler):
    def __init__(self) -> None:
        self.model_calls = 0

    def on_chat_model_start(self, *args, **kwargs) -> None:
        self.model_calls += 1


def stock_fake() -> dict[str, str]:
    out = {}
    for name, call in {
        "bind_tools": lambda m: m.bind_tools([record_lookup]),
        "with_structured_output": lambda m: m.with_structured_output(Draft),
    }.items():
        try:
            call(GenericFakeChatModel(messages=iter([tool_call("Draft", FORGED)])))
            out[name] = "supported"
        except Exception as exc:  # noqa: BLE001 - recording what the stock fake does
            out[name] = type(exc).__name__
    return out


async def main() -> None:
    print(f"langchain-core {md.version('langchain-core')}")
    print("for information, stock GenericFakeChatModel:", stock_fake())

    checks = {}
    m = ScriptedChatModel(script=[tool_call("record_lookup", {"metric": "revenue", "ticker": "IONQ"})])
    msg = m.bind_tools([record_lookup]).invoke("What was IonQ's revenue?")
    checks["a. tool call through bind_tools"] = msg.tool_calls[0]["name"] == "record_lookup"

    draft = ScriptedChatModel(script=[tool_call("Draft", FORGED)]).with_structured_output(Draft).invoke("draft")
    checks["b. structured output, sync"] = isinstance(draft, Draft) and draft.claims[0].kind == "magnitude"
    adraft = await ScriptedChatModel(script=[tool_call("Draft", FORGED)]).with_structured_output(Draft).ainvoke("draft")
    checks["b. structured output, async"] = isinstance(adraft, Draft) and adraft.claims[0].source_id == "chart-7"

    counter = CallCounter()
    primary = ScriptedChatModel(script=[RuntimeError("primary model down")])
    fallback = ScriptedChatModel(script=[AIMessage(content="answer from the fallback")])
    answer = await primary.with_fallbacks([fallback]).ainvoke("question", config={"callbacks": [counter]})
    checks["c. fallback used when the primary fails"] = answer.content == "answer from the fallback"
    checks["c. callback counted both model calls"] = counter.model_calls == 2

    for name, ok in checks.items():
        print(f"  {'PASS' if ok else 'FAIL'}  {name}")
    print("RESULT:", "PASS" if all(checks.values()) else "FAIL")


asyncio.run(main())
