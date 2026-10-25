"""Append-only audit log: one JSON line per request, every outcome, to a file
and to stdout. It is both the forensic record and a UI data source."""

import json
import os
import sys
import threading

AUDIT_PATH = os.environ.get("AUDIT_LOG_PATH", "/var/log/broker/audit.jsonl")
_lock = threading.Lock()


def write(record: dict) -> None:
    line = json.dumps(record, separators=(",", ":"))
    with _lock:
        os.makedirs(os.path.dirname(AUDIT_PATH), exist_ok=True)
        with open(AUDIT_PATH, "a") as f:  # append-only
            f.write(line + "\n")
    print("AUDIT " + line, file=sys.stdout, flush=True)


def tail(n: int = 200) -> list[dict]:
    try:
        with open(AUDIT_PATH) as f:
            lines = f.readlines()[-n:]
    except FileNotFoundError:
        return []
    return [json.loads(line) for line in lines if line.strip()]
