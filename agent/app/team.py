"""The agent team: an orchestrator that hands work to specialist sub-agents.

    supervise ─▶ delegate (one governed handoff per assignment) ─▶ synthesize

Every handoff is real:
  1. the orchestrator announces it      → agent_handoff     (agent event)
  2. the broker decides it               → delegation        (broker event)
     and, if allowed, mints the sub-agent a token that is narrower than the
     orchestrator's, bound to this user, and short-lived
  3. the sub-agent works with that token → request_sent / broker_check / decision ...
  4. it reports back                     → agent_handback    (agent event)

The orchestrator has no data access of its own and no knowledge of policy;
the broker decides every handoff and every request.
"""

import json
from typing import Annotated, Literal, TypedDict

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from pydantic import BaseModel, Field

from .broker_client import new_delegation_id, orchestrator
from .graph import NO_REPLY_NUDGE, PLAIN_REASONS, SCHEMA, _text, subagent_graph
from .hub import hub
from .llm import get_llm
from .presets import TEAM_AGENTS

SPECIALISTS = {a["id"]: a for a in TEAM_AGENTS if a["id"] != "orchestrator"}
MAX_ASSIGNMENTS = 4

SUPERVISOR_PROMPT = """\
You are the orchestrator of a small team of specialist AI agents, working for {name}. \
You never read data yourself: you hand tasks to specialists.

Your specialists:
{team}

Database schema (what exists — you do not know who is permitted to see what):
{schema}

Decide which specialists to hand work to for the user's latest message, in order. \
Give each a clear, self-contained task. Later specialists see the reports of earlier \
ones, so order them when one depends on another. If the user names a specialist, use \
that one. Use as few specialists as the task needs (at most {max}).
For greetings, small talk, or follow-up questions that can be answered from the \
conversation and earlier reports, return NO assignments.
{prior}"""

SUBAGENT_PROMPT = """\
You are the {agent_name} ({agent_id}), a specialist in a team of AI agents. {role}
The orchestrator has handed you this task, on behalf of {name}:

TASK: {task}
{context}
You can read company data only through the query_resource tool; a broker checks \
every request. Database schema:
{schema}

- Name every column explicitly ("*" is not supported). Request ONLY the columns your \
task needs — never "everything just in case". Filters are equality only.
- Never send the same request twice, and never send several identical requests at once.
- You cannot know in advance what you are permitted to do. If a request is denied, \
do not send it again or work around it; continue with what you can (for example \
the same table without the denied columns).
- When you are done, reply WITHOUT calling tools with a short factual report for the \
orchestrator: what you found (key facts, counts, names, figures) and exactly what was \
refused, with the broker's reason. Do not address the end user."""

SYNTH_PROMPT = """\
You are the orchestrator of a team of AI agents, now replying to {name}. The person \
typing to you IS {name}: speak to them as "you", never in the third person. Using their \
first name in a greeting is fine.

Write the reply to their latest message using your specialists' reports below. Briefly \
say who did what (e.g. "I asked our ticket analyst…"), then give the answer.

When something was refused, explain it in friendly, everyday, non-technical language:
- No table or column names, rule codes, "broker", "token" or "query" — unless the user \
explicitly asks for technical details.
- Explain why in plain words, based on the rule in the report:
  * user_rights → your account doesn't have access to that information (it may be \
limited to certain roles, such as HR);
  * agent_scope → that specialist isn't allowed to access that information for anyone \
— it's outside its job, not something about your account;
  * delegation → I wasn't allowed to hand that work (or that information) on to a \
specialist — certain work, like payments, is kept out of this team's hands;
  * token → a temporary sign-in problem on our side.
- Say what the team did manage, suggest a sensible next step (ask HR or a manager, \
request access, or use the proper system), and invite follow-up questions.
- Only offer help the team can actually give.
Greetings and small talk get a short, natural reply. Keep answers concise and well formatted.

Specialist reports for this turn:
{reports}

Reports from the previous turn (for follow-up questions):
{prior}"""


class Assignment(BaseModel):
    agent: Literal["ticket-analyst", "hr-analyst", "payroll-agent"] = Field(description="Which specialist")
    task: str = Field(description="A clear, self-contained task for that specialist")


class TeamPlan(BaseModel):
    assignments: list[Assignment] = Field(default_factory=list, description="Ordered handoffs; empty if none needed")


class TeamState(TypedDict):
    messages: Annotated[list, add_messages]   # the user-facing conversation only
    assignments: list[dict]
    reports: list[dict]
    prior_reports: list[dict]


def _ctx(config: RunnableConfig) -> tuple[str, str]:
    c = config["configurable"]
    return c["session_id"], c["on_behalf_of"]


def _format_reports(reports: list[dict]) -> str:
    if not reports:
        return "(none)"
    return "\n\n".join(
        f"- {SPECIALISTS[r['agent']]['name']} [{r['status']}] — task: {r['task']}\n  report: {r['summary']}" for r in reports
    )


async def supervise(state: TeamState, config: RunnableConfig):
    session_id, user = _ctx(config)
    team = "\n".join(f"- {a['id']}: {a['role']}" for a in SPECIALISTS.values())
    prior = state.get("prior_reports") or []
    system = SUPERVISOR_PROMPT.format(
        name=user.capitalize(), team=team, schema=SCHEMA, max=MAX_ASSIGNMENTS,
        prior=("\nReports from the previous turn:\n" + _format_reports(prior)) if prior else "",
    )
    plan: TeamPlan = await get_llm().with_structured_output(TeamPlan).ainvoke([SystemMessage(system), *state["messages"]])
    assignments = [a.model_dump() for a in plan.assignments[:MAX_ASSIGNMENTS]]
    if assignments:
        await hub.emit_agent_event("agent_plan", session_id, {
            "agent_id": "orchestrator",
            "steps": [f"Hand to {SPECIALISTS[a['agent']]['name']}: {a['task']}" for a in assignments],
            "assignments": assignments,
        })
    return {"assignments": assignments, "reports": []}


def _tool_outcomes(messages: list) -> tuple[int, list[dict]]:
    allowed, denials = 0, []
    for m in messages:
        if isinstance(m, ToolMessage):
            try:
                body = json.loads(m.content)
            except (TypeError, ValueError):
                continue
            if body.get("result") == "allow":
                allowed += 1
            elif body.get("result") in ("deny", "error"):
                denials.append({"rule_fired": body.get("rule_fired"), "reason": body.get("reason")})
    return allowed, denials


async def run_handoff(client, from_agent: str, agent_id: str, task: str, session_id: str, user: str,
                      context: str = "", delegation_id: str | None = None) -> dict:
    """One governed handoff, end to end: announce it, let the broker decide it,
    run the specialist with the delegated token, and report back."""
    delegation_id = delegation_id or new_delegation_id()
    await hub.emit_agent_event("agent_handoff", session_id, {
        "delegation_id": delegation_id, "from_agent": from_agent, "to_agent": agent_id, "task": task, "on_behalf_of": user})

    # The broker decides the handoff. on_behalf_of comes from the session.
    hub.expect_decision(delegation_id)
    try:
        d = await client.delegate(to_agent=agent_id, on_behalf_of=user, task=task,
                                  delegation_id=delegation_id, session_id=session_id)
    except Exception as e:
        d = {"result": "error", "reason": f"broker unreachable: {type(e).__name__}", "rule_fired": None}
    await hub.wait_decision(delegation_id)

    if d.get("result") != "allow":
        report = {"agent": agent_id, "task": task, "status": "refused",
                  "summary": f"Handoff refused by the broker ({d.get('rule_fired')}): {d.get('reason')}",
                  "denials": [{"rule_fired": d.get("rule_fired"), "reason": d.get("reason")}]}
    else:
        spec = SPECIALISTS[agent_id]
        sub_config = {
            "configurable": {
                "session_id": session_id, "on_behalf_of": user, "delegation_id": delegation_id,
                "agent_id": agent_id, "token": d["token"], "user_facing": False,
                "system_prompt": SUBAGENT_PROMPT.format(
                    agent_name=spec["name"], agent_id=agent_id, role=spec["role"], name=user.capitalize(),
                    task=task, context=context, schema=SCHEMA),
            },
            "recursion_limit": 30,
        }
        try:
            sub = await subagent_graph.ainvoke({"messages": [HumanMessage(task)], "plan": []}, sub_config)
            summary = next((_text(m) for m in reversed(sub["messages"]) if isinstance(m, AIMessage) and _text(m)), "")
            allowed, denials = _tool_outcomes(sub["messages"])
            status = "done" if not denials else ("partial" if allowed else "blocked")
        except Exception as e:
            summary, allowed, denials, status = f"(specialist failed: {type(e).__name__})", 0, [], "failed"
        report = {"agent": agent_id, "task": task, "status": status,
                  "summary": summary or "(no report)", "denials": denials}

    await hub.emit_agent_event("agent_handback", session_id, {
        "delegation_id": delegation_id, "from_agent": agent_id, "to_agent": from_agent,
        "status": report["status"], "summary": report["summary"][:1500],
    })
    return report


async def delegate(state: TeamState, config: RunnableConfig):
    session_id, user = _ctx(config)
    reports: list[dict] = []
    for a in state["assignments"]:
        context = ("\nReports from teammates so far:\n" + _format_reports(reports) + "\n") if reports else ""
        reports.append(await run_handoff(orchestrator, "orchestrator", a["agent"], a["task"], session_id, user, context))
    return {"reports": reports}


def _fallback(reports: list[dict]) -> str:
    rules = [d.get("rule_fired") for r in reports for d in r.get("denials", [])]
    if not rules:
        return "Sorry — I wasn't able to put an answer together that time. Could you ask again?"
    reasons = list(dict.fromkeys(PLAIN_REASONS.get(r, "something went wrong on our side") for r in rules))
    return "I'm sorry, the team couldn't fully do that, because " + "; and ".join(reasons) + ". Feel free to ask me why."


async def synthesize(state: TeamState, config: RunnableConfig):
    session_id, user = _ctx(config)
    system = SYNTH_PROMPT.format(name=user.capitalize(), reports=_format_reports(state.get("reports") or []),
                                 prior=_format_reports(state.get("prior_reports") or []))
    llm = get_llm()
    text = _text(await llm.ainvoke([SystemMessage(system), *state["messages"]]))
    if not text:
        text = _text(await llm.ainvoke([SystemMessage(system), *state["messages"], HumanMessage(NO_REPLY_NUDGE)])) \
            or _fallback(state.get("reports") or [])
    await hub.emit_agent_event("agent_message", session_id, {"text": text, "agent_id": "orchestrator"})
    return {"messages": [AIMessage(content=text)]}


def route_after_supervise(state: TeamState) -> str:
    return "delegate" if state["assignments"] else "synthesize"


def build_team_graph():
    g = StateGraph(TeamState)
    g.add_node("supervise", supervise)
    g.add_node("delegate", delegate)
    g.add_node("synthesize", synthesize)
    g.add_edge(START, "supervise")
    g.add_conditional_edges("supervise", route_after_supervise, {"delegate": "delegate", "synthesize": "synthesize"})
    g.add_edge("delegate", "synthesize")
    g.add_edge("synthesize", END)
    return g.compile()


team_graph = build_team_graph()
