"""Merged event stream for the browser.

Two sources feed one ordered stream:
  * the agent's own step events (agent_plan, agent_thinking, request_sent,
    result_returned, agent_message) — authored here via emit_agent_event();
  * the broker's events (broker_check, decision, delegation) — RELAYED verbatim from the
    broker's /events SSE stream. This service never authors a broker event.
"""

import asyncio
import json
from collections import deque
from datetime import datetime, timezone

import httpx
from fastapi import WebSocket

from .broker_client import BROKER_URL

AGENT_EVENT_TYPES = {"agent_plan", "agent_thinking", "request_sent", "result_returned", "agent_message",
                     "agent_handoff", "agent_handback", "agent_escalate", "agent_dispatch"}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class Hub:
    def __init__(self) -> None:
        self._clients: set[WebSocket] = set()
        self._recent: deque = deque(maxlen=500)
        self._decision_waiters: dict[str, asyncio.Event] = {}
        self.last_reply: dict[str, dict] = {}
        self._lock = asyncio.Lock()
        self.broker_stream_connected = False

    async def connect(self, ws: WebSocket) -> None:
        await ws.accept()
        self._clients.add(ws)

    def disconnect(self, ws: WebSocket) -> None:
        self._clients.discard(ws)

    async def _broadcast(self, event: dict) -> None:
        # One lock so every client sees the same total order of events.
        async with self._lock:
            self._recent.append(event)
            for ws in list(self._clients):
                try:
                    await ws.send_json(event)
                except Exception:
                    self._clients.discard(ws)

    async def emit_agent_event(self, type_: str, session_id: str, payload: dict) -> None:
        if type_ not in AGENT_EVENT_TYPES:
            raise ValueError(f"agent may not author '{type_}' events")
        event = {"type": type_, "ts": now_iso(), "session_id": session_id, "source": "agent", "payload": payload}
        if type_ == "agent_message":
            self.last_reply[session_id] = event  # lets a client that missed it over the socket recover it
        await self._broadcast(event)

    def expect_decision(self, request_id: str) -> asyncio.Event:
        """Register interest in the broker's decision for a request (before sending it)."""
        ev = asyncio.Event()
        self._decision_waiters[request_id] = ev
        return ev

    async def wait_decision(self, request_id: str, timeout: float = 3.0) -> None:
        """Hold result_returned until the broker's decision has been relayed, so
        the browser sees decision → result_returned in causal order."""
        ev = self._decision_waiters.get(request_id)
        if ev is None:
            return
        try:
            await asyncio.wait_for(ev.wait(), timeout)
        except asyncio.TimeoutError:
            pass
        finally:
            self._decision_waiters.pop(request_id, None)

    async def relay_broker_events(self) -> None:
        """Subscribe to the broker's SSE stream forever; relay each event unchanged."""
        while True:
            try:
                async with httpx.AsyncClient(timeout=None) as client:
                    async with client.stream("GET", f"{BROKER_URL}/events") as r:
                        self.broker_stream_connected = True
                        async for line in r.aiter_lines():
                            if not line.startswith("data: "):
                                continue
                            event = json.loads(line[6:])
                            await self._broadcast(event)
                            if event.get("type") in ("decision", "delegation"):
                                ev = self._decision_waiters.get(event["payload"].get("request_id"))
                                if ev:
                                    ev.set()
            except Exception:
                pass
            self.broker_stream_connected = False
            await asyncio.sleep(1)


hub = Hub()
