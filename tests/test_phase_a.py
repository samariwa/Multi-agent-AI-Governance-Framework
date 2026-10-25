"""Phase A — brokered access, no UI, no LLM."""

import json

from conftest import ENV, audit_records, broker_query, compose, exec_in, new_request_id

PROBE = """
import socket, sys
host, port = sys.argv[1], int(sys.argv[2])
try:
    socket.create_connection((host, port), timeout=3).close()
    print("CONNECTED")
except Exception as e:
    print("FAILED", type(e).__name__, e)
"""


def probe(service: str, host: str, port: int = 5432) -> str:
    return exec_in(service, "python", "-c", PROBE, host, str(port)).stdout.strip()


def container_ip_on(service: str, network_suffix: str) -> str:
    cid = compose("ps", "-q", service).stdout.strip()
    import subprocess
    nets = json.loads(subprocess.run(["docker", "inspect", "-f", "{{json .NetworkSettings.Networks}}", cid], capture_output=True, text=True).stdout)
    return next(v["IPAddress"] for k, v in nets.items() if k.endswith(network_suffix))


def test_a1_in_scope_read_returns_rows(token):
    r = broker_query(token, on_behalf_of="alice", table="tickets", columns=["id", "subject", "status"], filter={"status": "open"})
    assert r["result"] == "allow"
    assert r["row_count"] == len(r["rows"]) > 0
    assert all(row["status"] == "open" for row in r["rows"])
    assert set(r["rows"][0]) == {"id", "subject", "status"}


def test_a1_scripted_client_runs_from_agent_container():
    out = exec_in("agent", "python", "-m", "app.scripted_client").stdout
    results = json.loads(out.split("JSON ", 1)[1])
    by_label = {r["label"]: r["response"]["result"] for r in results}
    assert by_label == {"in-scope read": "allow", "write attempt": "deny", "out-of-scope column": "deny", "user lacks rights": "deny"}


def test_a2_write_request_denied_by_agent_scope_and_logged(token):
    r = broker_query(token, on_behalf_of="alice", table="tickets", columns=["status"], filter={"id": 1}, action="update", values={"status": "closed"})
    assert r["result"] == "deny" and r["rule_fired"] == "agent_scope"
    assert "rows" not in r
    assert any(a["request_id"] == r["request_id"] and a["result"] == "deny" for a in audit_records())


def test_a2_out_of_scope_column_denied_and_logged(token):
    r = broker_query(token, on_behalf_of="alice", table="employees", columns=["name", "bank_account"])
    assert r["result"] == "deny" and r["rule_fired"] == "agent_scope"
    assert "bank_account" in r["reason"]
    assert any(a["request_id"] == r["request_id"] for a in audit_records())


def test_a2_unknown_table_and_injection_shaped_names_denied(token):
    for table, cols in [("pg_shadow", ["usename"]), ("tickets; DROP TABLE tickets", ["id"]), ("tickets", ["id, salary"])]:
        r = broker_query(token, on_behalf_of="alice", table=table, columns=cols)
        assert r["result"] == "deny" and r["rule_fired"] == "agent_scope", (table, cols, r)


def test_a2_filter_on_unreadable_column_denied(token):
    # Filtering is reading: carol may not read customer_name, so may not filter on it either.
    r = broker_query(token, on_behalf_of="carol", table="tickets", columns=["id"], filter={"customer_name": "Nimbus Foods"})
    assert r["result"] == "deny" and r["rule_fired"] == "user_rights"


def test_a3_agent_has_no_route_to_db_but_broker_does():
    # By service name: the agent cannot even resolve "db".
    assert probe("agent", "db").startswith("FAILED")
    # By raw IP on the backend network: still unreachable (no shared network).
    db_ip = container_ip_on("db", "_backend")
    assert probe("agent", db_ip).startswith("FAILED")
    # Via the host: the db publishes no port, so there is no host-side path either.
    # (We check the db's published ports rather than probing host.docker.internal:5432,
    # because an unrelated Postgres on the developer's machine may listen there —
    # the agent's general egress is a separate, later governance concern.)
    assert compose("port", "db", "5432", check=False).stdout.strip() in ("", ":0")
    # The broker, on the backend network, can reach it.
    assert probe("broker", "db") == "CONNECTED"


def test_a3_broker_is_the_only_service_on_both_networks():
    import subprocess
    memberships = {}
    for service in ["db", "broker", "agent", "ui"]:
        cid = compose("ps", "-q", service).stdout.strip()
        if not cid:
            continue
        nets = json.loads(subprocess.run(["docker", "inspect", "-f", "{{json .NetworkSettings.Networks}}", cid], capture_output=True, text=True).stdout)
        memberships[service] = {k.rsplit("_", 1)[-1] for k in nets}
    assert memberships["db"] == {"backend"}
    assert memberships["broker"] == {"backend", "frontend"}
    assert "backend" not in memberships["agent"]
    assert [s for s, n in memberships.items() if n >= {"backend", "frontend"}] == ["broker"]


def test_a4_agent_image_has_no_db_driver_or_connection_string():
    pip = exec_in("agent", "pip", "list", "--format=freeze").stdout.lower()
    for driver in ["psycopg", "asyncpg", "pg8000", "sqlalchemy", "psycopg2"]:
        assert f"{driver}==" not in pip, driver
    imports = exec_in("agent", "python", "-c", "import importlib.util as u; print([m for m in ['psycopg','psycopg2','asyncpg','pg8000','sqlalchemy'] if u.find_spec(m)])")
    assert imports.stdout.strip() == "[]"

    env = exec_in("agent", "env").stdout
    assert ENV["POSTGRES_PASSWORD"] not in env
    assert "postgres://" not in env and "postgresql://" not in env
    for key in ["DB_HOST", "DB_PASSWORD", "POSTGRES_PASSWORD", "DATABASE_URL", "BROKER_SIGNING_SECRET"]:
        assert f"{key}=" not in env, key

    grep = exec_in("agent", "grep", "-rIl", "-e", ENV["POSTGRES_PASSWORD"], "-e", "postgresql://", "/srv", "/etc")
    assert grep.stdout.strip() == ""


def test_a5_expired_token_rejected():
    expired = exec_in("broker", "python", "-m", "app.agent_token", "--ttl", "-60").stdout.strip()
    r = broker_query(expired, on_behalf_of="alice", table="tickets", columns=["id"])
    assert r["result"] == "deny" and r["rule_fired"] == "token"
    assert "expired" in r["reason"]


def test_a5_tampered_token_rejected(token):
    body, sig = token.split(".")
    forged = body + "." + ("A" if sig[0] != "A" else "B") + sig[1:]
    r = broker_query(forged, on_behalf_of="alice", table="tickets", columns=["id"])
    assert r["result"] == "deny" and r["rule_fired"] == "token"


def test_a6_every_request_allow_and_deny_is_audited(token):
    expired = exec_in("broker", "python", "-m", "app.agent_token", "--ttl", "-1").stdout.strip()
    sent = {
        broker_query(token, on_behalf_of="bob", table="tickets", columns=["id"])["request_id"]: "allow",
        broker_query(token, on_behalf_of="bob", table="employees", columns=["salary"])["request_id"]: "deny",
        broker_query(expired, on_behalf_of="bob", table="tickets", columns=["id"])["request_id"]: "deny",
    }
    records = {r["request_id"]: r for r in audit_records()}
    for request_id, result in sent.items():
        rec = records[request_id]
        assert rec["result"] == result
        for field in ["ts", "request_id", "agent_id", "on_behalf_of", "resource", "action", "result", "rule_fired", "reason"]:
            assert field in rec, field
    # And it is on disk, append-only JSON lines, inside the broker.
    log = exec_in("broker", "cat", "/var/log/broker/audit.jsonl").stdout
    for request_id in sent:
        assert request_id in log
