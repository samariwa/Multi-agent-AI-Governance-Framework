"""Phase C — event streaming: one merged, ordered stream; decisions come from the broker."""

import asyncio
import json
import threading
import uuid

import httpx
import websockets

from conftest import BROKER, WS, broker_query, exec_in, objective, of_type, run_task

STAGES = ["token", "agent_scope", "user_rights"]


def assert_request_sequences(events):
    """For every request: request_sent → broker_check(s) in stage order → decision → result_returned."""
    types = [e["type"] for e in events]
    assert types[0] == "agent_plan", types[:3]
    assert types[-1] == "agent_message", types[-3:]
    for sent in of_type(events, "request_sent"):
        rid = sent["payload"]["request_id"]
        seq = [e for e in events if e["payload"].get("request_id") == rid]
        kinds = [e["type"] for e in seq]
        checks = [e["payload"] for e in seq if e["type"] == "broker_check"]
        decision = next(e["payload"] for e in seq if e["type"] == "decision")
        if decision["result"] == "allow":
            assert kinds == ["request_sent", "broker_check", "broker_check", "broker_check", "decision", "result_returned"], kinds
            assert [c["stage"] for c in checks] == STAGES and all(c["passed"] for c in checks)
        else:
            # Evaluation stops at the first failing stage.
            assert kinds[0] == "request_sent" and kinds[-2:] == ["decision", "result_returned"], kinds
            assert [c["stage"] for c in checks] == STAGES[: len(checks)]
            assert checks[-1]["passed"] is False and checks[-1]["stage"] == decision["rule_fired"]


def test_c1_full_task_produces_ordered_event_sequence(llm_ready):
    events = run_task("alice", objective("clean"))["events"]
    assert of_type(events, "request_sent")
    assert_request_sequences(events)


def test_c1_denied_task_produces_ordered_event_sequence(llm_ready):
    events = run_task("bob", objective("compensation"))["events"]
    assert any(e["payload"]["result"] == "deny" for e in of_type(events, "decision"))
    assert_request_sequences(events)


def _collect_broker_sse(stop: threading.Event, out: list):
    with httpx.stream("GET", f"{BROKER}/events", timeout=None) as r:
        for line in r.iter_lines():
            if line.startswith("data: "):
                out.append(json.loads(line[6:]))
            if stop.is_set():
                return


def test_c2_relayed_decisions_are_the_brokers_own_no_llm(token):
    """Drive the broker directly; the agent service's WebSocket must deliver the
    broker's decision verbatim (same payload, source=broker)."""
    session_id = f"s_c2_{uuid.uuid4().hex[:8]}"
    stop, from_broker = threading.Event(), []
    t = threading.Thread(target=_collect_broker_sse, args=(stop, from_broker), daemon=True)
    t.start()

    async def go():
        async with websockets.connect(WS) as ws:
            await asyncio.sleep(0.5)
            r1 = broker_query(token, on_behalf_of="bob", table="employees", columns=["salary"], session_id=session_id)
            r2 = broker_query(token, on_behalf_of="bob", table="tickets", columns=["id"], session_id=session_id)
            got = []
            while len([e for e in got if e["type"] == "decision"]) < 2:
                e = json.loads(await asyncio.wait_for(ws.recv(), 10))
                if e.get("session_id") == session_id:
                    got.append(e)
            return got, {r1["request_id"], r2["request_id"]}

    relayed, ids = asyncio.run(go())
    stop.set()
    relayed_decisions = [e for e in relayed if e["type"] == "decision"]
    broker_decisions = [e for e in from_broker if e["type"] == "decision" and e.get("session_id") == session_id]
    assert {e["payload"]["request_id"] for e in relayed_decisions} == ids
    assert all(e["source"] == "broker" for e in relayed if e["type"] in ("decision", "broker_check"))
    assert relayed_decisions == broker_decisions, "relay must not alter the broker's events"


def test_c2_agent_service_refuses_to_author_broker_events():
    code = (
        "import asyncio\nfrom app.hub import hub\n"
        "try:\n    asyncio.run(hub.emit_agent_event('decision', 's', {}))\n    print('AUTHORED')\n"
        "except ValueError:\n    print('REFUSED')\n"
    )
    assert exec_in("agent", "python", "-c", code).stdout.strip() == "REFUSED"


def test_c2_decisions_in_task_run_match_broker_stream(llm_ready):
    stop, from_broker = threading.Event(), []
    t = threading.Thread(target=_collect_broker_sse, args=(stop, from_broker), daemon=True)
    t.start()
    run = run_task("bob", objective("compensation"))
    stop.set()
    relayed = [e for e in run["events"] if e["type"] == "decision"]
    direct = [e for e in from_broker if e["type"] == "decision" and e.get("session_id") == run["session_id"]]
    assert relayed and all(e["source"] == "broker" for e in relayed)
    assert relayed == direct
