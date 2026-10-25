"""Adaptive orchestration: one main agent decides, step by step, whether to do
the work itself or hand parts of it to specialists — in parallel.

    main ⇄ tools        tools = its own query_resource calls
                                + hand_off calls (run concurrently)

Nothing is scripted: how many specialists run (none, one, two, three…), which
ones, and whether to send follow-up work after their reports are all the
model's choice. Every handoff and every data request is still decided by the
broker; the main agent delegates with its own token, so the chain is
assistant-agent → specialist and the delegation ceiling is its `delegable`.
"""

import asyncio
import json
from typing import Annotated, Literal, TypedDict

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from pydantic import BaseModel, Field, ValidationError

from .broker_client import AGENT_ID, broker, new_delegation_id
from .graph import NO_REPLY_NUDGE, QUERY_TOOL, SCHEMA, SYSTEM_PROMPT, _fallback_reply, _text, tools_node
from .hub import hub
from .llm import get_llm
from .team import SPECIALISTS, run_handoff

MAX_PARALLEL = 4        # handoffs in one step
MAX_HANDOFFS = 8        # handoffs per user message

TEAM_PROMPT = """

You also lead a team of specialist agents. Add a specialist ONLY when it is needed:
- Default to doing the work yourself. Greetings, follow-ups, and any part you can cover \
with one or two quick lookups of your own: use query_resource, no specialists.
- Add a specialist only for a part that needs real analysis in its area, or that only \
that specialist handles (for example payments). One specialist per such part, never two \
of the same kind, and never one "just in case". A task with one such part gets one \
specialist; two parts get two; three get three.
- Independent parts: call hand_off for each in the SAME step — they run in parallel.
- If one part needs another's result, hand off the first, wait for its report, then hand \
off the next with the facts it needs written into its task.
- Write each task precisely: say exactly which information it needs, nothing more.
- Each specialist reports back to you. Then you may hand off follow-up work, fetch \
something yourself, or answer. Never repeat a handoff that was refused, in this turn \
or later in the conversation.

Your specialists:
{team}

In your reply, say briefly who did what ("I asked our HR analyst…"). Plain meanings \
for refusals involving the team:
  * delegation → I wasn't allowed to pass that work (or that information) on to a \
specialist — certain work, like payments, is kept out of this team's hands;
  * agent_scope on a specialist's request → that specialist isn't allowed to access \
that for anyone; it's outside its job."""


class HandOffArgs(BaseModel):
    agent: Literal["ticket-analyst", "hr-analyst", "payroll-agent"] = Field(description="Which specialist")
    task: str = Field(description="A clear, self-contained task, including any facts it needs from earlier reports")


HANDOFF_TOOL = {
    "type": "function",
    "function": {
        "name": "hand_off",
        "description": "Hand a sub-task to a specialist agent. Several calls in one step run in parallel. "
                       "Returns the specialist's report, or the reason the handoff was refused.",
        "parameters": HandOffArgs.model_json_schema(),
    },
}


class AdaptiveState(TypedDict):
    messages: Annotated[list, add_messages]
    handoffs: int


def _ctx(config: RunnableConfig) -> tuple[str, str]:
    c = config["configurable"]
    return c["session_id"], c["on_behalf_of"]


def _system(user: str) -> str:
    team = "\n".join(f"- {a['id']}: {a['role']}" for a in SPECIALISTS.values())
    base = SYSTEM_PROMPT.format(user=user, name=user.capitalize(), schema=SCHEMA,
                                plan="(none — decide as you go, using the team only when needed)")
    return base + TEAM_PROMPT.format(team=team)


async def main_node(state: AdaptiveState, config: RunnableConfig):
    session_id, user = _ctx(config)
    system = _system(user)
    response: AIMessage = await get_llm().bind_tools([QUERY_TOOL, HANDOFF_TOOL]).ainvoke([SystemMessage(system), *state["messages"]])
    text = _text(response)
    if response.tool_calls:
        if text:
            await hub.emit_agent_event("agent_thinking", session_id, {"text": text, "agent_id": AGENT_ID})
        return {"messages": [response]}
    if not text:
        retry: AIMessage = await get_llm().ainvoke([SystemMessage(system), *state["messages"], HumanMessage(NO_REPLY_NUDGE)])
        text = _text(retry) or _fallback_reply(state["messages"])
        response = AIMessage(content=text)
    await hub.emit_agent_event("agent_message", session_id, {"text": text, "agent_id": AGENT_ID})
    return {"messages": [response]}


def _report_message(call_id: str, report: dict) -> ToolMessage:
    return ToolMessage(json.dumps({
        "specialist": report["agent"], "status": report["status"], "report": report["summary"],
        "refused": report["denials"],
    }), tool_call_id=call_id)


async def tools(state: AdaptiveState, config: RunnableConfig):
    session_id, user = _ctx(config)
    calls = state["messages"][-1].tool_calls
    queries = [c for c in calls if c["name"] == "query_resource"]
    handoffs = [c for c in calls if c["name"] == "hand_off"]
    results: list[ToolMessage] = []

    # Validate and cap the handoffs; anything over the cap is answered, not dropped.
    used = state.get("handoffs") or 0
    runnable = []
    for c in handoffs:
        try:
            args = HandOffArgs.model_validate(c["args"])
        except ValidationError as e:
            results.append(ToolMessage(json.dumps({"error": f"invalid arguments: {e.errors()[:3]}"}), tool_call_id=c["id"]))
            continue
        if len(runnable) >= MAX_PARALLEL or used + len(runnable) >= MAX_HANDOFFS:
            results.append(ToolMessage(json.dumps({"error": "handoff limit reached; finish with the reports you have"}), tool_call_id=c["id"]))
            continue
        runnable.append((c["id"], args, new_delegation_id()))

    if runnable:
        await hub.emit_agent_event("agent_dispatch", session_id, {
            "agent_id": AGENT_ID, "parallel": len(runnable),
            "handoffs": [{"delegation_id": d, "agent": a.agent, "task": a.task} for _, a, d in runnable],
        })

    async def own_queries():
        if not queries:
            return []
        out = await tools_node({"messages": [AIMessage(content="", tool_calls=queries)]}, config)
        return out["messages"]

    reports, own = await asyncio.gather(
        asyncio.gather(*(run_handoff(broker, AGENT_ID, a.agent, a.task, session_id, user, delegation_id=d)
                         for _, a, d in runnable)),
        own_queries(),
    )
    results += [_report_message(cid, r) for (cid, _, _), r in zip(runnable, reports)]
    results += own
    return {"messages": results, "handoffs": used + len(runnable)}


def route(state: AdaptiveState) -> str:
    last = state["messages"][-1]
    return "tools" if isinstance(last, AIMessage) and last.tool_calls else END


def build_adaptive_graph():
    g = StateGraph(AdaptiveState)
    g.add_node("main", main_node)
    g.add_node("tools", tools)
    g.add_edge(START, "main")
    g.add_conditional_edges("main", route, {"tools": "tools", END: END})
    g.add_edge("tools", "main")
    return g.compile()


adaptive_graph = build_adaptive_graph()
