"""Phase G — agent team: governed handoffs (delegation) between agents.

Broker-level tests need no LLM; the task-run tests at the bottom drive the real
orchestrator + sub-agents through the UI API.
"""

import re

import httpx
import pytest

from conftest import BROKER, ENV, audit_records, broker_query, exec_in, final_text, new_request_id, of_type, run_task

TEAM = ["ticket-analyst", "hr-analyst", "payroll-agent"]


@pytest.fixture(scope="module")
def orch_token() -> str:
    r = httpx.post(f"{BROKER}/token", json={"agent_id": "orchestrator", "client_secret": ENV["ORCHESTRATOR_CLIENT_SECRET"]})
    r.raise_for_status()
    return r.json()["token"]


def delegate(token, to_agent, user, task="test task"):
    r = httpx.post(f"{BROKER}/delegate", json={"token": token, "to_agent": to_agent, "on_behalf_of": user, "task": task})
    r.raise_for_status()
    return r.json()


# ---------- broker-level (no LLM) ----------

def test_g1_sub_agents_cannot_get_tokens_of_their_own():
    for agent in TEAM:
        r = httpx.post(f"{BROKER}/token", json={"agent_id": agent, "client_secret": ENV["ORCHESTRATOR_CLIENT_SECRET"]})
        assert r.status_code in (401, 403), (agent, r.status_code)


def test_g2_orchestrator_has_no_direct_data_access(orch_token):
    r = broker_query(orch_token, on_behalf_of="alice", table="tickets", columns=["id"])
    assert r["result"] == "deny" and r["rule_fired"] == "agent_scope"


def test_g3_handoff_mints_a_narrower_user_bound_token(orch_token):
    d = delegate(orch_token, "hr-analyst", "alice", "staffing review")
    assert d["result"] == "allow" and d["chain"] == ["orchestrator", "hr-analyst"]
    child = d["token"]
    ok = broker_query(child, on_behalf_of="alice", table="employees", columns=["name", "salary"])
    assert ok["result"] == "allow" and ok["row_count"] == 12
    # The token is bound to Alice: the sub-agent cannot switch users.
    switched = broker_query(child, on_behalf_of="bob", table="employees", columns=["name"])
    assert switched["result"] == "deny" and switched["rule_fired"] == "delegation"
    # Audited with the full chain.
    rec = {r["request_id"]: r for r in audit_records()}[ok["request_id"]]
    assert rec["delegation_chain"] == ["orchestrator", "hr-analyst"] and rec["agent_id"] == "hr-analyst"
    assert rec_of(d["delegation_id"])["action"] == "delegate"


def rec_of(request_id):
    return {r["request_id"]: r for r in audit_records()}[request_id]


def test_g3_user_rights_still_apply_through_the_chain(orch_token):
    child = delegate(orch_token, "hr-analyst", "bob")["token"]
    r = broker_query(child, on_behalf_of="bob", table="employees", columns=["name", "salary"])
    assert r["result"] == "deny" and r["rule_fired"] == "user_rights"


def test_g4_handoff_to_an_undelegable_agent_is_refused(orch_token):
    d = delegate(orch_token, "payroll-agent", "alice", "pay bonuses")
    assert d["result"] == "deny" and d["rule_fired"] == "delegation" and "token" not in d
    rec = rec_of(d["delegation_id"])
    assert rec["result"] == "deny" and rec["resource"]["delegate_to"] == "payroll-agent"


def test_g5_permissions_narrow_down_the_chain(orch_token):
    child = delegate(orch_token, "ticket-analyst", "alice")["token"]
    # In ticket-analyst's own scope, but not in what the orchestrator may delegate.
    r = broker_query(child, on_behalf_of="alice", table="tickets", columns=["id", "customer_name"])
    assert r["result"] == "deny" and r["rule_fired"] == "delegation" and "customer_name" in r["reason"]
    # Within both: allowed.
    r = broker_query(child, on_behalf_of="alice", table="tickets", columns=["id", "subject"])
    assert r["result"] == "allow"
    # Another specialist's data: refused by the sub-agent's own scope, even for Alice.
    r = broker_query(child, on_behalf_of="alice", table="employees", columns=["name", "salary"])
    assert r["result"] == "deny" and r["rule_fired"] == "agent_scope"


def test_g6_sub_agents_cannot_re_delegate_or_loop(orch_token):
    child = delegate(orch_token, "hr-analyst", "alice")["token"]
    d = delegate(child, "ticket-analyst", "alice")
    assert d["result"] == "deny" and d["rule_fired"] == "delegation"
    d = delegate(orch_token, "orchestrator", "alice")
    assert d["result"] == "deny" and d["rule_fired"] == "delegation"


def test_g7_expired_delegator_cannot_hand_off_and_child_never_outlives_parent(orch_token):
    expired = exec_in("broker", "python", "-m", "app.agent_token", "--agent", "orchestrator", "--ttl", "-5").stdout.strip()
    d = delegate(expired, "hr-analyst", "alice")
    assert d["result"] == "deny" and d["rule_fired"] == "token"
    import base64, json
    parent_exp = json.loads(base64.urlsafe_b64decode(orch_token.split(".")[0] + "=="))["exp"]
    assert delegate(orch_token, "hr-analyst", "alice")["exp"] <= parent_exp


# ---------- task runs through the real agent team (LLM) ----------

def team_task(user, objective_id):
    from conftest import presets
    text = next(o["text"] for o in presets()["team_objectives"] if o["id"] == objective_id)
    return run_task(user, text, mode="team")


def test_g8_staffing_review_hands_work_to_both_specialists(llm_ready):
    events = team_task("alice", "staffing")["events"]
    handoffs = of_type(events, "agent_handoff")
    assert {h["payload"]["to_agent"] for h in handoffs} >= {"ticket-analyst", "hr-analyst"}
    delegations = of_type(events, "delegation")
    assert delegations and all(e["source"] == "broker" for e in delegations)
    # Each handoff: announced → broker decides → sub-agent works → hands back.
    for h in handoffs:
        did = h["payload"]["delegation_id"]
        kinds = [e["type"] for e in events if e["payload"].get("delegation_id") == did or e["payload"].get("request_id") == did]
        assert kinds[0] == "agent_handoff" and "delegation" in kinds and kinds[-1] == "agent_handback", kinds
    requests = of_type(events, "request_sent")
    assert requests and all(r["payload"]["agent_id"] in TEAM for r in requests), "only sub-agents touch data"
    for d in of_type(events, "decision"):
        assert d["payload"]["delegation_chain"][0] == "orchestrator"
    assert events[-1]["type"] == "agent_message"


def test_g9_staffing_review_as_bob_does_tickets_and_explains_staff_part(llm_ready):
    events = team_task("bob", "staffing")["events"]
    ds = [e["payload"] for e in of_type(events, "decision")]
    assert any(d["result"] == "allow" and d["agent_id"] == "ticket-analyst" for d in ds)
    assert any(d["result"] == "deny" and d["agent_id"] == "hr-analyst" and d["rule_fired"] == "user_rights" for d in ds)
    text = final_text(events).lower()
    assert "user_rights" not in text and "broker" not in text
    assert re.search(r"\byou(r)?\b", text)


def test_g10_handoff_to_payroll_is_refused_and_explained(llm_ready):
    events = team_task("alice", "payroll")["events"]
    refused = [e["payload"] for e in of_type(events, "delegation") if e["payload"]["to_agent"] == "payroll-agent"]
    assert refused and all(d["result"] == "deny" and d["rule_fired"] == "delegation" for d in refused)
    assert not any(e["payload"].get("agent_id") == "payroll-agent" for e in of_type(events, "request_sent"))
    assert final_text(events).strip()
