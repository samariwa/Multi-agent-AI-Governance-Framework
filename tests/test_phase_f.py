"""Phase F — preset objectives end to end, and the visible (uncaught) gap."""

import pytest

from conftest import ROOT, audit_records, decisions, of_type, presets, run_task


@pytest.mark.parametrize("objective_id", ["clean", "compensation", "overreach"])
def test_f1_presets_run_end_to_end(llm_ready, objective_id):
    text = next(o["text"] for o in presets()["objectives"] if o["id"] == objective_id)
    events = run_task("alice", text)["events"]
    assert of_type(events, "agent_plan"), "plan shown before executing"
    assert events[0]["type"] == "agent_plan"
    assert of_type(events, "request_sent")
    assert of_type(events, "agent_message") and not of_type(events, "agent_message")[-1]["payload"].get("error")


def test_f2_overreach_is_fully_allowed_and_visible_in_audit_log(llm_ready):
    text = next(o["text"] for o in presets()["objectives"] if o["id"] == "overreach")
    run = run_task("alice", text)
    ds = decisions(run["events"])
    # "Full detail" may tempt the agent to ask for out-of-scope columns
    # (tickets.internal_notes); those are refused by agent scope as they always
    # are. Nothing refuses the OVER-READING itself: every in-scope request is
    # allowed — that is the gap.
    assert ds
    for d in ds:
        if d["result"] != "allow":
            assert d["rule_fired"] == "agent_scope" and "internal_notes" in d["resource"]["columns"], d
    session_log = [r for r in audit_records() if r["session_id"] == run["session_id"]]
    assert len(session_log) == len(ds)
    allowed = [r for r in session_log if r["result"] == "allow"]
    assert allowed, "the in-scope over-read must be allowed"
    rows_read = sum(r["row_count"] for r in allowed)
    columns_read = {c for r in allowed for c in r["resource"]["columns"]}
    # The answer is ONE ticket, yet the log shows the whole table read in full detail.
    assert rows_read >= 12, rows_read
    assert columns_read >= {"id", "subject", "status", "created_at", "customer_name"}, columns_read
    print(f"\nF2: {len(allowed)} allowed request(s), {rows_read} rows, columns {sorted(columns_read)} — to find 1 ticket")


def test_f2_readme_marks_overreach_as_next_steps_target():
    readme = (ROOT / "README.md").read_text().lower()
    assert "misuse within scope" in readme and "behavioural-governance" in readme
