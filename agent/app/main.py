"""Agent service + UI backend.

  POST /api/session   {user, mode}      -> start a session acting for a user (mode: single | team | adaptive)
  POST /api/chat      {session_id, message}  -> run the agent (events stream over /ws)
  GET  /ws                                -> merged live event stream (agent + broker)
  GET  /api/audit                         -> broker audit log (proxied, read-only)
  GET  /api/token                         -> current agent token expiry (for the countdown)
  GET  /api/presets                       -> preset users + objectives for the UI

The UI only asserts who the user is (by picking a preset). The session stores
that identity and the agent stamps it into every broker request; the LLM never
sees or sets it as a parameter. Whether that user may do anything is decided
solely by the broker.
"""

import asyncio
import os
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from langchain_core.messages import HumanMessage
from pydantic import BaseModel

from . import llm
from .broker_client import AGENT_ID, broker, orchestrator
from .graph import graph
from .hub import hub
from .presets import ADAPTIVE_OBJECTIVES, OBJECTIVES, TEAM_AGENTS, TEAM_OBJECTIVES, USERS
from .adaptive import adaptive_graph
from .team import team_graph

sessions: dict[str, dict] = {}
RUN_TIMEOUT_SECONDS = int(os.environ.get("AGENT_RUN_TIMEOUT_SECONDS", "480"))


@asynccontextmanager
async def lifespan(app: FastAPI):
    relay = asyncio.create_task(hub.relay_broker_events())
    yield
    relay.cancel()


app = FastAPI(title="Agent service", lifespan=lifespan)


class SessionRequest(BaseModel):
    user: str
    mode: str = "single"   # single (assistant; may escalate) | team (orchestrator + specialists) | adaptive (assistant leads specialists, in parallel)


class ChatRequest(BaseModel):
    session_id: str
    message: str


@app.get("/api/health")
def health():
    return {
        "ok": True,
        "agent_id": AGENT_ID,
        "llm_provider": llm.provider(),
        "llm_model": llm.model_name(),
        "broker_stream_connected": hub.broker_stream_connected,
    }


@app.get("/api/presets")
def presets():
    return {"users": USERS, "objectives": OBJECTIVES, "team_agents": TEAM_AGENTS, "team_objectives": TEAM_OBJECTIVES,
            "adaptive_objectives": ADAPTIVE_OBJECTIVES}


@app.post("/api/session")
def create_session(req: SessionRequest):
    session_id = f"s_{uuid.uuid4().hex[:10]}"
    mode = req.mode if req.mode in ("team", "adaptive") else "single"
    sessions[session_id] = {"user": req.user, "mode": mode, "messages": [], "reports": [], "busy": False}
    return {"session_id": session_id, "user": req.user, "mode": mode}


def _team_input(messages: list, session: dict) -> dict:
    return {"messages": messages, "assignments": [], "reports": [], "prior_reports": session["reports"]}


async def _turn(session: dict, message: str, config: dict) -> dict:
    """Team sessions always use the orchestrator. Single sessions start with the
    assistant, whose planner escalates complex tasks to the agent team."""
    messages = [*session["messages"], HumanMessage(message)]
    if session["mode"] == "team":
        return await team_graph.ainvoke(_team_input(messages, session), config)
    if session["mode"] == "adaptive":
        return await adaptive_graph.ainvoke({"messages": messages, "handoffs": 0}, config)
    state = await graph.ainvoke({"messages": messages, "plan": [], "use_team": False}, config)
    if state.get("use_team"):
        return await team_graph.ainvoke(_team_input(state["messages"], session), config)
    return state


async def _run(session_id: str, message: str) -> None:
    session = sessions[session_id]
    config = {
        "configurable": {"session_id": session_id, "on_behalf_of": session["user"]},
        "recursion_limit": 80,
    }
    try:
        state = await asyncio.wait_for(_turn(session, message, config), RUN_TIMEOUT_SECONDS)
        session["messages"] = state["messages"]
        if "reports" in state:
            session["reports"] = state["reports"] or []
    except Exception as e:
        reason = (f"the model didn't finish within {RUN_TIMEOUT_SECONDS}s" if isinstance(e, asyncio.TimeoutError)
                  else f"{type(e).__name__}: {e}")
        await hub.emit_agent_event("agent_message", session_id, {
            "text": f"Sorry — something went wrong on my side ({reason}). Any requests I made are in the audit log. "
                    "Please try again or ask me something else.",
            "error": True,
        })
    finally:
        session["busy"] = False


@app.get("/api/session/{session_id}")
def session_status(session_id: str):
    session = sessions.get(session_id)
    if session is None:
        raise HTTPException(404, "unknown session")
    return {"session_id": session_id, "user": session["user"], "mode": session["mode"], "busy": session["busy"],
            "last_reply": hub.last_reply.get(session_id)}


@app.post("/api/chat", status_code=202)
async def chat(req: ChatRequest):
    session = sessions.get(req.session_id)
    if session is None:
        raise HTTPException(404, "unknown session")
    if session["busy"]:
        raise HTTPException(409, "agent is already working on a task in this session")
    session["busy"] = True
    asyncio.create_task(_run(req.session_id, req.message))
    return {"accepted": True, "session_id": req.session_id, "on_behalf_of": session["user"]}


@app.get("/api/audit")
async def audit(n: int = 200):
    return {"records": await broker.audit(n)}


@app.get("/api/token")
async def token_info(agent: str = AGENT_ID):
    client = orchestrator if agent == "orchestrator" else broker
    await client.token()  # refreshes if close to expiry
    return {"agent_id": client.agent_id, "exp": client.token_exp}


@app.websocket("/ws")
async def ws(websocket: WebSocket):
    await hub.connect(websocket)
    try:
        while True:
            await websocket.receive_text()  # keep the connection open; clients don't send
    except WebSocketDisconnect:
        pass
    finally:
        hub.disconnect(websocket)
