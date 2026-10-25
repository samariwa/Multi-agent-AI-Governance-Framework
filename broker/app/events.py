"""In-process event bus. The broker publishes broker_check/decision events here;
GET /events streams them (Server-Sent Events) to subscribers such as the agent
service's UI backend, which relays them to the browser unchanged."""

import asyncio
from datetime import datetime, timezone


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class EventBus:
    def __init__(self) -> None:
        self._subscribers: set[asyncio.Queue] = set()

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=1000)
        self._subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._subscribers.discard(q)

    def emit(self, type_: str, session_id: str | None, payload: dict) -> dict:
        event = {
            "type": type_,
            "ts": now_iso(),
            "session_id": session_id,
            "source": "broker",
            "payload": payload,
        }
        for q in list(self._subscribers):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                pass  # slow subscriber; drop rather than block enforcement
        return event


bus = EventBus()
