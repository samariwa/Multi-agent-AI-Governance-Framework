"""Phase A scripted client: exercises the broker with fixed structured requests,
no LLM. Runs inside the agent container (same network position, same image —
no DB driver, no connection string).

    docker compose exec agent python -m app.scripted_client
"""

import asyncio
import json

from .broker_client import broker

SCRIPT = [
    ("in-scope read", dict(on_behalf_of="alice", table="tickets", columns=["id", "subject", "status"], filter={"status": "open"})),
    ("write attempt", dict(on_behalf_of="alice", table="tickets", columns=["status"], filter={"id": 1}, action="update", values={"status": "closed"})),
    ("out-of-scope column", dict(on_behalf_of="alice", table="employees", columns=["name", "bank_account"])),
    ("user lacks rights", dict(on_behalf_of="bob", table="employees", columns=["name", "salary"])),
]


async def main() -> None:
    results = []
    for label, req in SCRIPT:
        r = await broker.query(**req)
        results.append({"label": label, "request": req, "response": r})
        print(f"{label:22} -> {r['result']:5} {r.get('reason') or ''} {('rows=' + str(r['row_count'])) if 'row_count' in r else ''}")
    print("JSON " + json.dumps(results))


if __name__ == "__main__":
    asyncio.run(main())
