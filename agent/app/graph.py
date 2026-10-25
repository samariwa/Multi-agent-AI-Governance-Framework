"""The LangGraph agent: plan → act ⇄ tools → final answer.

Its single tool, query_resource, hands a structured request to the broker.
Deliberately, nothing here filters what the LLM asks for: out-of-scope reads
and write attempts are sent to the broker and refused THERE, so enforcement
is visible. The acting user (on_behalf_of) comes from the session context in
the run config — never from model output.
"""

import json
from typing import Annotated, Literal, TypedDict

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from pydantic import BaseModel, Field, ValidationError

from .broker_client import AGENT_ID, broker, new_request_id
from .hub import hub
from .llm import get_llm

# The agent knows the database schema (what exists), not the policy (what is
# allowed). Only the broker knows and enforces the policy.
SCHEMA = """\
tickets(id, subject, status, created_at, customer_name, internal_notes)
  status is one of: open, pending, closed
employees(id, name, department, salary, bank_account)"""

SYSTEM_PROMPT = """\
You are a friendly workplace assistant chatting with {name}. The person typing \
to you IS {name}: speak to them directly as "you", never in the third person (say \
"you don't have access", never "user '{user}' is not permitted"). Using their \
first name in a greeting is fine. You can read company information only through \
the query_resource tool, which sends a request to a data broker that checks \
permissions.

Database schema:
{schema}

How to work:
- Request the tables and columns the task needs, naming every column explicitly \
("*" is not supported). Use filters where helpful (equality only, e.g. {{"status": "open"}}).
- For follow-up questions, use what is already in the conversation; only call the \
tool again if you genuinely need new data.
- You cannot know in advance what you are permitted to do. The broker decides \
every request. If a request is denied, do not send the same request again and do \
not try to obtain the denied data another way. Carry on with the rest of the task: \
make further requests for the data you still need, leaving out whatever was denied.

When something is denied, FIRST finish everything you still can: if only some \
columns were refused, immediately call the tool again for the same table without \
those columns, and fetch any other data the task needs. Only after that, in your \
final reply, explain the refusal in friendly, everyday, non-technical language — \
never leave the user guessing:
- Do NOT use technical terms in your reply: no table or column names, no rule \
codes (like user_rights or agent_scope), no "broker", "request", "query", \
"token" or "denied permission to read". Describe the information in plain words \
("employee salary details", "staff bank details", "internal notes on tickets").
- Explain why, in plain words, based on the rule the broker gave:
  * user_rights → your account doesn't have access to that information \
(e.g. it is restricted to certain roles, such as HR).
  * agent_scope → I'm not able to access or change that for anyone — it's \
outside what I'm allowed to do as an assistant, not something about your account.
  * token → I had a temporary sign-in problem on my side; trying again may work.
- Say what you were able to do, and suggest a helpful next step (for example, \
asking a manager or HR, requesting access through the usual process, or \
something related you can help with right now).
- Be warm and brief, and invite follow-up questions.
- Follow-up questions like "why?" or "what exactly was denied?" still get a \
plain-language answer (what kind of information, and that it's restricted for \
your account or outside what I can do). Only mention technical details (table \
or column names, rule codes) if the user explicitly asks for them, e.g. "which \
table?" or "give me the technical details".
- Read the broker's reason carefully to see how much was refused. If it names a \
whole table (e.g. "not permitted to read table 'employees'"), then ALL of that kind \
of information is off-limits for this user — not just the part you asked for — \
so say e.g. "you don't have access to employee information" rather than only \
"salary information". If it names specific columns, only those are off-limits.
- Only offer help you can actually give: never suggest information you have \
already been refused in this conversation (if all employee information was \
refused, don't offer employee names, departments or a staff list).

Example — if a request for employee salaries is refused because of the user's access:
"I'm sorry, I can't put together the compensation review for you. Salary \
information is restricted, and your account doesn't have access to it — it's \
usually limited to HR. If you need this, HR or your manager could help, or you \
could request access. In the meantime, I can help with things you do have access \
to, like a summary of open support tickets. Want me to do that?"

Greetings and small talk ("hi", "thanks", "how are you"): reply briefly and \
naturally, like a friendly colleague — e.g. "Hi {name}! What can I help you \
with?" Don't call any tools, don't describe your capabilities, and never use \
stiff phrases like "company data", "data broker" or "query".

Always finish your turn with a message to the user. Keep answers concise and well formatted.

Your plan for this turn:
{plan}"""

PLAN_PROMPT = """\
You are planning the next turn for an assistant that can read company data only \
via a data broker (tool: query_resource). Database schema:
{schema}

If answering the user's latest message needs data fetched, set needs_data=true and \
break it into 2-6 short, concrete steps (data to fetch, then how to use it). If it \
is a greeting, small talk, thanks, or a conversational follow-up that can be \
answered from the conversation so far (e.g. "hi", "why was that denied?"), set \
needs_data=false and give a single step. Do not execute anything.

Also decide use_team. Set use_team=true only for a complex task that needs the \
specialist team: it combines support-ticket analysis WITH staff/HR information (for \
example matching tickets to people, or workload versus headcount or pay), it involves \
payments, payroll or bank details, or the user asks for the team or a specialist. \
Simple questions about one kind of data, greetings and follow-ups: use_team=false."""

NO_REPLY_NUDGE = "Now write your reply to the user. Do not call any tools."


class Plan(BaseModel):
    needs_data: bool = Field(description="Whether new data must be fetched to answer")
    steps: list[str] = Field(description="Ordered, concrete steps")
    use_team: bool = Field(default=False, description="Whether to hand this to the specialist team")


class QueryResourceArgs(BaseModel):
    table: str = Field(description="Table name, e.g. 'tickets'")
    columns: list[str] = Field(description="Explicit column names to read (or to write, for write actions); '*' is not supported")
    filter: dict[str, str | int | float | bool | None] = Field(default_factory=dict, description="Equality filters, column -> value")
    limit: int | None = Field(default=None, description="Max rows to return")
    action: Literal["read", "insert", "update", "delete"] = Field(default="read", description="Operation to perform")
    values: dict | None = Field(default=None, description="New values, for write actions only")


QUERY_TOOL = {
    "type": "function",
    "function": {
        "name": "query_resource",
        "description": "Send a structured data request to the broker. Returns rows on allow, or a deny with the reason.",
        "parameters": QueryResourceArgs.model_json_schema(),
    },
}


class State(TypedDict):
    messages: Annotated[list, add_messages]
    plan: list[str]
    use_team: bool


def _ctx(config: RunnableConfig) -> tuple[str, str]:
    c = config["configurable"]
    return c["session_id"], c["on_behalf_of"]


def _identity(config: RunnableConfig) -> tuple[str, str | None]:
    """Which agent is running this node, and the token it acts with. The single
    assistant uses its own token; a team sub-agent uses the delegated token the
    broker minted at handoff."""
    c = config["configurable"]
    return c.get("agent_id", AGENT_ID), c.get("token")


def _text(message: AIMessage) -> str:
    if isinstance(message.content, str):
        return message.content.strip()
    return "\n".join(b.get("text", "") for b in message.content if isinstance(b, dict) and b.get("type") == "text").strip()


async def plan_node(state: State, config: RunnableConfig):
    session_id, _ = _ctx(config)
    llm = get_llm().with_structured_output(Plan)
    plan: Plan = await llm.ainvoke([SystemMessage(PLAN_PROMPT.format(schema=SCHEMA)), *state["messages"]])
    # A complex task is escalated to the agent team (main._run runs team_graph next).
    if plan.use_team and plan.needs_data:
        await hub.emit_agent_event("agent_escalate", session_id, {
            "from_agent": AGENT_ID, "to_agent": "orchestrator",
            "reason": "This task needs several specialists, so it goes to the agent team.",
        })
        return {"plan": [], "use_team": True}
    # A plan card is shown whenever data will be fetched; a purely conversational
    # follow-up ("why was that denied?") goes straight to the answer.
    if plan.needs_data and plan.steps:
        await hub.emit_agent_event("agent_plan", session_id, {"steps": plan.steps, "agent_id": AGENT_ID})
    return {"plan": plan.steps, "use_team": False}


def _denials_this_turn(messages: list) -> list[dict]:
    """Broker denials since the user's latest message (for the no-reply fallback)."""
    out = []
    for m in reversed(messages):
        if isinstance(m, HumanMessage):
            break
        if isinstance(m, ToolMessage):
            try:
                body = json.loads(m.content)
            except (TypeError, ValueError):
                continue
            if body.get("result") in ("deny", "error"):
                out.append(body)
    return list(reversed(out))


PLAIN_REASONS = {
    "user_rights": "your account doesn't have access to some of the information this needs",
    "agent_scope": "part of it is outside what I'm allowed to do as an assistant, for anyone",
    "token": "I had a temporary sign-in problem on my side (trying again may work)",
    "delegation": "that part needed help I'm not allowed to pass the work on for",
}


def _fallback_reply(messages: list) -> str:
    denials = _denials_this_turn(messages)
    if not denials:
        return "Sorry — I wasn't able to come up with an answer that time. Could you rephrase it or ask again?"
    reasons = list(dict.fromkeys(PLAIN_REASONS.get(d.get("rule_fired"), "something went wrong on my side") for d in denials))
    return ("I'm sorry, I couldn't fully do that for you, because " + "; and ".join(reasons) + ".\n\n"
            "If you need it, a manager or HR may be able to help. Feel free to ask me why, "
            "or ask for something else I can help with.")


async def act_node(state: State, config: RunnableConfig):
    session_id, user = _ctx(config)
    agent_id, _ = _identity(config)
    # A team sub-agent brings its own system prompt and reports to the
    # orchestrator rather than replying to the user.
    user_facing = config["configurable"].get("user_facing", True)
    role = "main" if user_facing else "sub"
    llm = get_llm(role).bind_tools([QUERY_TOOL])
    system = config["configurable"].get("system_prompt") or SYSTEM_PROMPT.format(
        user=user, name=user.capitalize(), schema=SCHEMA, plan="\n".join(f"{i}. {s}" for i, s in enumerate(state["plan"], 1)))
    response: AIMessage = await llm.ainvoke([SystemMessage(system), *state["messages"]])
    text = _text(response)
    # Reasoning models (e.g. gemma4 via Ollama with thinking on) return their
    # reasoning separately; it is real model output, so show it as a step.
    reasoning = (response.additional_kwargs.get("reasoning_content") or "").strip()
    if reasoning:
        await hub.emit_agent_event("agent_thinking", session_id, {"text": reasoning[:1200] + ("…" if len(reasoning) > 1200 else ""), "agent_id": agent_id})
    if response.tool_calls:
        if text:
            await hub.emit_agent_event("agent_thinking", session_id, {"text": text, "agent_id": agent_id})
        return {"messages": [response]}

    # Final turn: the user must always get a reply, never silence.
    if not text:
        retry: AIMessage = await get_llm(role).ainvoke([SystemMessage(system), *state["messages"], HumanMessage(NO_REPLY_NUDGE)])
        text = _text(retry) or _fallback_reply(state["messages"])
        response = AIMessage(content=text)
    if user_facing:
        await hub.emit_agent_event("agent_message", session_id, {"text": text})
    return {"messages": [response]}


async def tools_node(state: State, config: RunnableConfig):
    session_id, on_behalf_of = _ctx(config)
    agent_id, token = _identity(config)
    user_facing = config["configurable"].get("user_facing", True)
    results = []
    for call in state["messages"][-1].tool_calls:
        try:
            args = QueryResourceArgs.model_validate(call["args"])
        except ValidationError as e:
            results.append(ToolMessage(json.dumps({"error": f"invalid arguments: {e.errors()[:3]}"}), tool_call_id=call["id"]))
            continue

        request_id = new_request_id()
        await hub.emit_agent_event("request_sent", session_id, {
            "request_id": request_id, "table": args.table, "columns": args.columns, "action": args.action,
            "filter": args.filter, "limit": args.limit, "on_behalf_of": on_behalf_of, "agent_id": agent_id,
            "delegation_id": config["configurable"].get("delegation_id"),
        })
        hub.expect_decision(request_id)
        try:
            response = await broker.query(
                on_behalf_of=on_behalf_of,  # from the session, never from the model
                table=args.table, columns=args.columns, filter=args.filter, limit=args.limit,
                action=args.action, values=args.values, request_id=request_id, session_id=session_id,
                token=token,
            )
        except Exception as e:
            await hub.wait_decision(request_id, timeout=0.5)
            await hub.emit_agent_event("result_returned", session_id, {"request_id": request_id, "error": f"broker unreachable: {type(e).__name__}"})
            results.append(ToolMessage(json.dumps({"error": "broker unreachable"}), tool_call_id=call["id"]))
            continue

        await hub.wait_decision(request_id)
        if response.get("result") == "allow":
            await hub.emit_agent_event("result_returned", session_id, {"request_id": request_id, "row_count": response["row_count"]})
            content = {"result": "allow", "row_count": response["row_count"], "rows": response["rows"]}
        else:
            await hub.emit_agent_event("result_returned", session_id, {"request_id": request_id, "error": response.get("reason")})
            # The deny reason is passed through so the agent can explain it —
            # see the DECISION note in broker/app/main.py about what this leaks.
            content = {
                "result": response.get("result"), "reason": response.get("reason"), "rule_fired": response.get("rule_fired"),
                # Generic guidance only — no policy knowledge. Small models tend to
                # abandon the whole task on the first deny without this nudge.
                "next_step": "Do not request the denied items again. If only some columns were refused and the task "
                             "still needs the rest, call query_resource again NOW for the same table without the denied "
                             "columns, and fetch any other data the task needs, before replying. Then, in your final answer, "
                             + ("explain to the user in plain, friendly, non-technical words what you couldn't get and why."
                                if user_facing else "report exactly what was refused and the broker's reason."),
            }
        results.append(ToolMessage(json.dumps(content, default=str), tool_call_id=call["id"]))
    return {"messages": results}


def route_after_act(state: State) -> str:
    last = state["messages"][-1]
    return "tools" if isinstance(last, AIMessage) and last.tool_calls else END


def build_graph():
    g = StateGraph(State)
    g.add_node("plan", plan_node)
    g.add_node("act", act_node)
    g.add_node("tools", tools_node)
    g.add_edge(START, "plan")
    g.add_conditional_edges("plan", lambda s: END if s.get("use_team") else "act", {"act": "act", END: END})
    g.add_conditional_edges("act", route_after_act, {"tools": "tools", END: END})
    g.add_edge("tools", "act")
    return g.compile()


def build_subagent_graph():
    """A team specialist: act ⇄ tools until it has a report. The orchestrator
    already planned, so there is no plan step."""
    g = StateGraph(State)
    g.add_node("act", act_node)
    g.add_node("tools", tools_node)
    g.add_edge(START, "act")
    g.add_conditional_edges("act", route_after_act, {"tools": "tools", END: END})
    g.add_edge("tools", "act")
    return g.compile()


graph = build_graph()
subagent_graph = build_subagent_graph()
