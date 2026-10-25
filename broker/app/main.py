"""The broker: the sole enforcement point and the sole holder of DB credentials.

Every POST /query is evaluated in order — token → (delegation, for delegated
tokens) → agent scope → user rights —
with a broker_check event per stage, a final decision event, and an audit
record in every outcome. Only if all checks pass does the broker build
parameterised SQL and run it on its own connection.
"""

import asyncio
import hmac
import json
import os
import re
import time
import uuid

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from . import agent_token, audit, db
from .events import bus, now_iso
from .policy import get_policy

app = FastAPI(title="Broker")

REQUEST_ID_RE = re.compile(r"^req_[A-Za-z0-9_-]{4,64}$")
DELEGATION_ID_RE = re.compile(r"^del_[A-Za-z0-9_-]{4,64}$")


class TokenRequest(BaseModel):
    agent_id: str
    client_secret: str


class QueryRequest(BaseModel):
    token: str = ""
    on_behalf_of: str = ""
    table: str = ""
    columns: list[str] = Field(default_factory=list)
    filter: dict = Field(default_factory=dict)
    limit: int | None = None
    action: str = "read"            # read | insert | update | delete — only read is ever executed
    values: dict | None = None      # payload for write actions (never executed; see policy)
    request_id: str | None = None   # caller may pre-assign so it can announce the request first
    session_id: str | None = None   # correlates events with a UI session


@app.get("/health")
def health():
    return {"ok": True}


@app.post("/token")
def issue_token(req: TokenRequest):
    """Client-credentials style issuance: the agent proves who it is and gets a
    short-lived signed token. The signing secret never leaves the broker."""
    expected = os.environ.get("AGENT_CLIENT_SECRETS", "")
    # Format: "agent-id=secret,other-agent=secret2"
    registry = dict(pair.split("=", 1) for pair in expected.split(",") if "=" in pair)
    secret = registry.get(req.agent_id)
    if secret is None or not hmac.compare_digest(secret, req.client_secret):
        raise HTTPException(status_code=401, detail="unknown agent or bad client secret")
    if get_policy().is_delegated_only(req.agent_id):
        raise HTTPException(status_code=403, detail=f"agent '{req.agent_id}' may only act via delegation")
    token, payload = agent_token.mint(req.agent_id)
    return {"token": token, "exp": payload["exp"], "iat": payload["iat"], "agent_id": req.agent_id}


class DelegateRequest(BaseModel):
    token: str = ""                 # the delegator's own token
    to_agent: str = ""
    on_behalf_of: str = ""
    task: str = ""
    delegation_id: str | None = None
    session_id: str | None = None


@app.post("/delegate")
def delegate(req: DelegateRequest):
    """A handoff: one agent asks the broker for a token that lets a sub-agent
    work on a task, for the same user. The broker decides; the sub-agent's
    token is narrower (its own scope ∩ the delegator's ceiling), bound to the
    user, and never outlives the delegator's token."""
    delegation_id = req.delegation_id if req.delegation_id and DELEGATION_ID_RE.match(req.delegation_id) else f"del_{uuid.uuid4().hex[:12]}"
    policy = get_policy()
    chain: list[str] = []

    def check(stage: str, passed: bool):
        bus.emit("broker_check", req.session_id, {"request_id": delegation_id, "stage": stage, "passed": passed, "kind": "delegation"})

    def finish(result: str, rule_fired: str | None, reason: str, token: str | None = None, exp: int | None = None):
        frm = chain[-1] if chain else None
        new_chain = chain + [req.to_agent] if chain else None
        bus.emit("delegation", req.session_id, {
            "request_id": delegation_id, "result": result, "reason": reason, "rule_fired": rule_fired,
            "from_agent": frm, "to_agent": req.to_agent, "chain": new_chain, "on_behalf_of": req.on_behalf_of,
            "task": req.task[:300], "exp": exp,
        })
        audit.write({
            "ts": now_iso(), "request_id": delegation_id, "session_id": req.session_id,
            "agent_id": frm, "delegation_chain": new_chain, "on_behalf_of": req.on_behalf_of,
            "resource": {"delegate_to": req.to_agent, "table": "", "columns": [], "task": req.task[:300]},
            "action": "delegate", "result": result, "rule_fired": rule_fired, "reason": reason, "row_count": None,
        })
        if result == "allow":
            return {"delegation_id": delegation_id, "result": "allow", "token": token, "exp": exp, "chain": new_chain}
        return {"delegation_id": delegation_id, "result": "deny", "reason": reason, "rule_fired": rule_fired}

    try:
        claims = agent_token.verify(req.token)
        chain = list(claims.get("chain") or [claims["agent_id"]])
        check("token", True)
    except agent_token.TokenError as e:
        check("token", False)
        return finish("deny", "token", str(e))

    verdict = policy.check_delegation(chain, req.to_agent)
    if verdict.allowed and claims.get("on_behalf_of") not in (None, req.on_behalf_of):
        verdict = type(verdict)(False, "delegation", f"token is bound to user '{claims['on_behalf_of']}', not '{req.on_behalf_of}'")
    check("delegation", verdict.allowed)
    if not verdict.allowed:
        return finish("deny", verdict.rule_fired, verdict.reason)

    exp = min(claims["exp"], int(time.time()) + policy.delegated_token_ttl)
    token, payload = agent_token.mint(req.to_agent, exp=exp, extra={
        "chain": chain + [req.to_agent], "on_behalf_of": req.on_behalf_of, "delegation_id": delegation_id,
    })
    return finish("allow", None, f"'{chain[-1]}' may delegate to '{req.to_agent}' for '{req.on_behalf_of}'", token, payload["exp"])


@app.post("/query")
async def query(req: QueryRequest):
    request_id = req.request_id if req.request_id and REQUEST_ID_RE.match(req.request_id) else f"req_{uuid.uuid4().hex[:12]}"
    policy = get_policy()
    filter_cols = list(req.filter.keys())
    # Filter columns count as accessed columns: filtering on a column you may
    # not read would let you probe its values (e.g. salary = 90000 → 1 row).
    accessed = list(dict.fromkeys(req.columns + filter_cols))
    resource = {"table": req.table, "columns": req.columns, "filter_columns": filter_cols}
    agent_id = None
    chain: list[str] | None = None

    def check(stage: str, passed: bool):
        bus.emit("broker_check", req.session_id, {"request_id": request_id, "stage": stage, "passed": passed})

    def finish(result: str, rule_fired: str | None = None, reason: str | None = None, rows: list | None = None, extra: dict | None = None):
        decision = {
            "request_id": request_id,
            "result": result,
            "reason": reason,
            "rule_fired": rule_fired,
            "resource": resource,
            "on_behalf_of": req.on_behalf_of,
            "agent_id": agent_id,
            "delegation_chain": chain,
            "action": req.action,
        }
        if rows is not None:
            decision["row_count"] = len(rows)
        if extra:
            decision.update(extra)
        bus.emit("decision", req.session_id, decision)
        audit.write({
            "ts": now_iso(),
            "request_id": request_id,
            "session_id": req.session_id,
            "agent_id": agent_id,
            "delegation_chain": chain,
            "on_behalf_of": req.on_behalf_of,
            "resource": resource,
            "action": req.action,
            "result": result,
            "rule_fired": rule_fired,
            "reason": reason,
            "row_count": len(rows) if rows is not None else None,
            **(extra or {}),
        })
        if result == "allow":
            return {"request_id": request_id, "result": "allow", "rows": rows, "row_count": len(rows), **(extra or {})}
        # DECISION (conscious, marked choice): on a deny the broker returns an
        # explicit "not permitted" reason naming the table/columns, so the agent
        # can explain the refusal to the user. This LEAKS THE EXISTENCE of the
        # data (Bob learns there is an employees.salary column). The stricter
        # alternative is to respond as if the resource does not exist (e.g. a
        # generic "unknown resource"), which hides it entirely at the cost of a
        # less explainable experience. This demo deliberately chooses
        # explainability.
        return {"request_id": request_id, "result": result, "reason": reason, "rule_fired": rule_fired}

    # Stage 1 — token: valid signature, not expired.
    try:
        claims = agent_token.verify(req.token)
        agent_id = claims["agent_id"]
        chain = claims.get("chain")
        check("token", True)
    except agent_token.TokenError as e:
        check("token", False)
        return finish("deny", "token", str(e))

    # Stage 1b — delegation (delegated tokens only): every link in the chain is
    # still permitted, the user is the one the chain was created for, and the
    # request fits every delegator's ceiling. Permissions only ever narrow.
    if chain:
        verdict = policy.check_delegated_access(chain, claims.get("on_behalf_of"), req.on_behalf_of, req.action, req.table, accessed)
        check("delegation", verdict.allowed)
        if not verdict.allowed:
            return finish("deny", verdict.rule_fired, verdict.reason)

    # Basic shape of the structured request (still default-deny, still audited).
    if not req.table or not req.columns or not all(isinstance(c, str) for c in req.columns):
        check("agent_scope", False)
        return finish("deny", "agent_scope", "request must name a table and at least one column")
    if any(not isinstance(v, (str, int, float, bool, type(None))) for v in req.filter.values()):
        check("agent_scope", False)
        return finish("deny", "agent_scope", "filter values must be scalars (equality match only)")

    # Stage 2 — agent scope: table + columns + action ⊆ the agent's scope.
    verdict = policy.check_agent_scope(claims["scope_ref"], req.action, req.table, accessed)
    check("agent_scope", verdict.allowed)
    if not verdict.allowed:
        return finish("deny", verdict.rule_fired, verdict.reason)

    # Stage 3 — user rights: table + columns ⊆ the acting user's may_read.
    verdict = policy.check_user_rights(req.on_behalf_of, req.action, req.table, accessed)
    check("user_rights", verdict.allowed)
    if not verdict.allowed:
        return finish("deny", verdict.rule_fired, verdict.reason)

    # All checks passed. Only reads exist past this point (policy grants no
    # writes); default-deny anything else rather than guess.
    if req.action != "read":
        return finish("deny", "agent_scope", f"action '{req.action}' is not supported")

    limit = policy.row_limit(req.on_behalf_of, req.limit)
    try:
        rows = await db.run_select(req.table, req.columns, req.filter, limit)
    except Exception as e:  # surface DB errors without leaking internals
        return finish("error", "execution", f"query failed: {type(e).__name__}")
    return finish("allow", None, "permitted by agent scope ∩ user rights", rows, {"limit_applied": limit})


@app.get("/audit")
def get_audit(n: int = 200):
    return {"records": audit.tail(n)}


@app.get("/events")
async def events(request: Request):
    """Server-Sent Events stream of every broker event."""
    q = bus.subscribe()

    async def stream():
        try:
            yield ": connected\n\n"
            while True:
                if await request.is_disconnected():
                    break
                try:
                    event = await asyncio.wait_for(q.get(), timeout=15)
                    yield f"data: {json.dumps(event)}\n\n"
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
        finally:
            bus.unsubscribe(q)

    return StreamingResponse(stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})
