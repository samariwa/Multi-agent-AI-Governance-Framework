"""The agent's only way to reach data: structured requests to the broker.

This module holds no DB driver, no connection string, and no policy. It gets
a short-lived signed token from the broker (client-credentials style) and
sends structured requests; the broker decides and runs the query.
"""

import os
import time
import uuid

import httpx

BROKER_URL = os.environ.get("BROKER_URL", "http://broker:8000")
AGENT_ID = os.environ.get("AGENT_ID", "assistant-agent")
REFRESH_MARGIN_SECONDS = 60


def new_request_id() -> str:
    return f"req_{uuid.uuid4().hex[:12]}"


def new_delegation_id() -> str:
    return f"del_{uuid.uuid4().hex[:12]}"


class BrokerClient:
    """One client per agent identity that holds its own credential
    (assistant-agent, orchestrator). Team sub-agents have no credential: they
    act with a delegated token passed per request."""

    def __init__(self, agent_id: str = AGENT_ID, secret_env: str = "AGENT_CLIENT_SECRET") -> None:
        self.agent_id = agent_id
        self._secret_env = secret_env
        self._token: str | None = None
        self._exp: int = 0
        self._http = httpx.AsyncClient(base_url=BROKER_URL, timeout=30)

    @property
    def token_exp(self) -> int:
        return self._exp

    async def token(self) -> str:
        if self._token is None or self._exp - time.time() < REFRESH_MARGIN_SECONDS:
            r = await self._http.post("/token", json={
                "agent_id": self.agent_id,
                "client_secret": os.environ[self._secret_env],
            })
            r.raise_for_status()
            body = r.json()
            self._token, self._exp = body["token"], body["exp"]
        return self._token

    async def query(self, *, on_behalf_of: str, table: str, columns: list[str], filter: dict | None = None,
                    limit: int | None = None, action: str = "read", values: dict | None = None,
                    request_id: str | None = None, session_id: str | None = None, token: str | None = None) -> dict:
        payload = {
            "token": token or await self.token(),
            "on_behalf_of": on_behalf_of,
            "table": table,
            "columns": columns,
            "filter": filter or {},
            "limit": limit,
            "action": action,
            "values": values,
            "request_id": request_id or new_request_id(),
            "session_id": session_id,
        }
        r = await self._http.post("/query", json=payload)
        r.raise_for_status()
        return r.json()

    async def delegate(self, *, to_agent: str, on_behalf_of: str, task: str,
                       delegation_id: str, session_id: str | None = None) -> dict:
        """Ask the broker to let `to_agent` work on `task` for `on_behalf_of`."""
        r = await self._http.post("/delegate", json={
            "token": await self.token(), "to_agent": to_agent, "on_behalf_of": on_behalf_of,
            "task": task, "delegation_id": delegation_id, "session_id": session_id,
        })
        r.raise_for_status()
        return r.json()

    async def audit(self, n: int = 200) -> list[dict]:
        r = await self._http.get("/audit", params={"n": n})
        r.raise_for_status()
        return r.json()["records"]


broker = BrokerClient()
orchestrator = BrokerClient("orchestrator", "ORCHESTRATOR_CLIENT_SECRET")
