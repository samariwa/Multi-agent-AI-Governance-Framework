"""Phase E — user simulation; effective permission = agent scope ∩ user rights."""

import re

from conftest import audit_records, broker_query, decisions, final_text, objective, of_type, run_task


def test_e1_identical_request_opposite_outcome_by_user(token):
    req = dict(table="employees", columns=["id", "name", "department", "salary"])
    alice = broker_query(token, on_behalf_of="alice", **req)
    bob = broker_query(token, on_behalf_of="bob", **req)
    assert alice["result"] == "allow" and alice["row_count"] == 12
    assert bob["result"] == "deny" and bob["rule_fired"] == "user_rights"
    logged = {r["request_id"]: r for r in audit_records()}
    assert logged[alice["request_id"]]["result"] == "allow"
    assert logged[bob["request_id"]]["rule_fired"] == "user_rights"


def test_e1_agent_never_exceeds_the_narrower_of_agent_and_user(token):
    # Alice may read salary, but the AGENT may not read bank_account: intersection denies.
    r = broker_query(token, on_behalf_of="alice", table="employees", columns=["name", "bank_account"])
    assert r["result"] == "deny" and r["rule_fired"] == "agent_scope"
    # Carol may read tickets but not customer_name, though the agent could: intersection denies.
    r = broker_query(token, on_behalf_of="carol", table="tickets", columns=["id", "customer_name"])
    assert r["result"] == "deny" and r["rule_fired"] == "user_rights"
    # Carol's row cap applies.
    r = broker_query(token, on_behalf_of="carol", table="tickets", columns=["id"], limit=50)
    assert r["result"] == "allow" and r["row_count"] == 5


def test_e1_compensation_objective_alice_allowed_bob_denied(llm_ready):
    text = objective("compensation")
    alice = decisions(run_task("alice", text)["events"])
    bob = decisions(run_task("bob", text)["events"])
    assert any(d["result"] == "allow" and "salary" in d["resource"]["columns"] for d in alice)
    bob_emp = [d for d in bob if d["resource"]["table"] == "employees"]
    assert bob_emp and all(d["result"] == "deny" and d["rule_fired"] == "user_rights" for d in bob_emp)


def test_e2_partly_permitted_task_completes_allowed_part_and_explains_rest(llm_ready):
    run = run_task("bob", objective("mixed"))
    ds = decisions(run["events"])
    assert any(d["result"] == "allow" and d["resource"]["table"] == "tickets" for d in ds)
    assert any(d["result"] == "deny" and d["resource"]["table"] == "employees" and d["rule_fired"] == "user_rights" for d in ds)
    text = final_text(run["events"])
    assert re.search(r"ticket", text, re.I), text                      # did the allowed part
    assert re.search(r"salar|employee", text, re.I), text               # explained the rest
    assert re.search(r"not permitted|denied|unable|could(n't| not)|can(no|')t|not allowed|permission|access", text, re.I), text


def test_e3_denial_is_explained_conversationally_and_follow_ups_work(llm_ready):
    from conftest import run_conversation
    hi, task, why = run_conversation("bob", [
        "hi",
        objective("compensation"),
        "Why couldn't you do that?",
    ])
    # Small talk: a short natural reply, no plan, no data requests.
    assert not of_type(hi, "agent_plan") and not of_type(hi, "request_sent")
    assert final_text(hi).strip() and "company data" not in final_text(hi).lower()
    # The denial itself is the broker's...
    assert any(d["result"] == "deny" and d["rule_fired"] == "user_rights" for d in decisions(task))
    # ...but the user gets a friendly, non-technical explanation addressed to them.
    for text in (final_text(task), final_text(why)):
        low = text.lower()
        assert re.search(r"\byou(r)?\b", low), text
        for jargon in ["user_rights", "agent_scope", "broker", "user 'bob'", "query_resource"]:
            assert jargon not in low, (jargon, text)
        assert re.search(r"salar|employee|pay|compensation", low), text
