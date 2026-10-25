"""Shared helpers for the acceptance tests. Run against the live stack:

    docker compose up -d --build
    make test            (or: .venv/bin/pytest tests)
"""

import asyncio
import json
import pathlib
import subprocess
import uuid

import httpx
import pytest
import websockets

ROOT = pathlib.Path(__file__).resolve().parent.parent
BROKER = "http://127.0.0.1:8001"
UI = "http://127.0.0.1:8080"
WS = "ws://127.0.0.1:8080/ws"
TASK_TIMEOUT = 600


def load_env() -> dict:
    env = {}
    for line in (ROOT / ".env").read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            env[k] = v
    return env


ENV = load_env()


def compose(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(["docker", "compose", *args], cwd=ROOT, capture_output=True, text=True, check=check)


def exec_in(service: str, *cmd: str) -> subprocess.CompletedProcess:
    return compose("exec", "-T", service, *cmd, check=False)


def new_request_id() -> str:
    return f"req_test_{uuid.uuid4().hex[:10]}"


@pytest.fixture(scope="session")
def token() -> str:
    r = httpx.post(f"{BROKER}/token", json={"agent_id": "assistant-agent", "client_secret": ENV["AGENT_CLIENT_SECRET"]})
    r.raise_for_status()
    return r.json()["token"]


def broker_query(token: str, **kw) -> dict:
    body = {"token": token, "request_id": new_request_id(), **kw}
    r = httpx.post(f"{BROKER}/query", json=body, timeout=30)
    r.raise_for_status()
    return r.json()


def audit_records(n: int = 1000) -> list[dict]:
    return httpx.get(f"{BROKER}/audit", params={"n": n}).json()["records"]


@pytest.fixture(scope="session")
def llm_ready():
    try:
        health = httpx.get(f"{UI}/api/health", timeout=5).json()
    except httpx.HTTPError:
        pytest.fail("UI/agent not reachable on :8080 — run `docker compose up -d --build`")
    if not health.get("llm_provider"):
        pytest.skip("No LLM API key configured in .env (ANTHROPIC_API_KEY or OPENAI_API_KEY)")
    return health


async def _run_task(user: str, text: str, timeout: float, mode: str = "single") -> dict:
    async with websockets.connect(WS, max_size=None) as ws:
        async with httpx.AsyncClient(base_url=UI, timeout=30) as http:
            session_id = (await http.post("/api/session", json={"user": user, "mode": mode})).json()["session_id"]
            r = await http.post("/api/chat", json={"session_id": session_id, "message": text})
            r.raise_for_status()
        events = []

        async def collect():
            while True:
                event = json.loads(await ws.recv())
                if event.get("session_id") != session_id:
                    continue
                events.append(event)
                if event["type"] == "agent_message":
                    return

        await asyncio.wait_for(collect(), timeout)
    return {"session_id": session_id, "events": events}


def run_task(user: str, text: str, timeout: float = TASK_TIMEOUT, mode: str = "single") -> dict:
    """Run one agent task via the same API + WebSocket the browser uses and
    return every event for that session, in arrival order."""
    return asyncio.run(_run_task(user, text, timeout, mode))


def of_type(events: list[dict], type_: str) -> list[dict]:
    return [e for e in events if e["type"] == type_]


def decisions(events: list[dict]) -> list[dict]:
    return [e["payload"] for e in of_type(events, "decision")]


def final_text(events: list[dict]) -> str:
    return of_type(events, "agent_message")[-1]["payload"]["text"]


def presets() -> dict:
    return httpx.get(f"{UI}/api/presets").json()


def objective(id_: str) -> str:
    return next(o["text"] for o in presets()["objectives"] if o["id"] == id_)


async def _run_conversation(user: str, turns: list[str], timeout: float) -> list[list[dict]]:
    """Several turns in ONE session (follow-ups see the earlier conversation)."""
    out = []
    async with websockets.connect(WS, max_size=None) as ws:
        async with httpx.AsyncClient(base_url=UI, timeout=30) as http:
            session_id = (await http.post("/api/session", json={"user": user})).json()["session_id"]
            for text in turns:
                for _ in range(20):  # the session frees up a moment after agent_message
                    r = await http.post("/api/chat", json={"session_id": session_id, "message": text})
                    if r.status_code != 409:
                        break
                    await asyncio.sleep(0.5)
                r.raise_for_status()
                events = []

                async def collect():
                    while True:
                        event = json.loads(await ws.recv())
                        if event.get("session_id") == session_id:
                            events.append(event)
                            if event["type"] == "agent_message":
                                return

                await asyncio.wait_for(collect(), timeout)
                out.append(events)
    return out


def run_conversation(user: str, turns: list[str], timeout: float = TASK_TIMEOUT) -> list[list[dict]]:
    return asyncio.run(_run_conversation(user, turns, timeout))
