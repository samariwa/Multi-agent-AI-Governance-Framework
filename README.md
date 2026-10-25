# Brokered Agent Access — a governance demonstrator

A local, Dockerised demo of a security architecture for AI agents. One LLM agent performs
tasks against a SQL database, but it **never touches the database**. It can only hand
structured requests to a **broker**. The broker decides what is allowed, holds the
credentials, and runs the query. A browser UI lets you switch between users with different
rights and watch every request travel through the broker and get allowed or denied, live.

> **The one idea:** the agent can't access the database incorrectly, because it doesn't access
> the database at all. Authority is bound to both the agent and the human it acts for. An action
> is allowed only if the agent's scope permits it **and** the acting user's rights permit it.

## Run it

```bash
cp .env.example .env        # then change the secrets
ollama pull gemma4:e4b      # local model (default); Ollama must be running on the host
docker compose up -d --build
open http://localhost:8080
```

| What | Where |
|---|---|
| UI (chat, user switcher, system view, audit log) | http://localhost:8080 |
| Broker (for tests/operators, loopback only) | http://127.0.0.1:8001 |
| Database | **not published.** Only the broker can reach it. |

By default the agent uses a **local model via Ollama** (`gemma4:e4b`), reached from the agent
container at `host.docker.internal:11434`. Swap with `LLM_PROVIDER` (`ollama` \| `anthropic` \| `openai`)
and `LLM_MODEL`. Any Ollama model with tool calling works (e.g. `qwen3:8b`).

### Tests

```bash
make test              # creates .venv, installs pytest + Playwright, runs tests/ against the running stack
make test-a            # Phase A only (no LLM needed)
```

Tests that need the LLM skip, and say why, when no LLM is configured. With a local 8B model each agent run takes about a minute, so the full suite takes a while.
Phase D drives a headless Chromium. Its screenshots are written to `tests/artifacts/`.

## Architecture

```
            frontend network                         backend network (internal)
  ┌─────────┐   ┌──────────────────────┐   ┌──────────┐            ┌──────────┐
  │ browser │──▶│ ui (nginx + React)   │──▶│  agent   │──structured─▶  broker  │──SQL──▶  db  │
  └─────────┘   └──────────────────────┘   │ LangGraph│  requests  │ FastAPI  │        │ Postgres │
                                           │ + UI API │◀──events───│ policy,  │        └──────────┘
                                           └──────────┘   (SSE)    │ audit    │
                                                │                  └──────────┘
                                                └── outbound internet (LLM API only)
```

| Service | Networks | Holds |
|---|---|---|
| `db` | backend | the data |
| `broker` | **backend + frontend** (the only one on both) | DB credentials, token-signing secret, policy, audit log |
| `agent` | frontend | its own client credential, LLM key. **No DB driver, no connection string.** |
| `ui` | frontend | nothing. It only asserts which user is acting. |

### Why the two networks are the security boundary

`db` sits only on `backend`, which is an `internal` Docker network. `agent` sits only on
`frontend`. The broker is the only container attached to both. So the agent has **no network
path to the database**. It can't resolve the name `db`, and it can't reach the db's IP. Even a
fully compromised agent holding a leaked connection string couldn't use it. This is
"enforcement in the execution path" in containerised form: the policy is enforced because the
only route to the data goes through the enforcer. Asking the agent to behave isn't the
mechanism. Test A3 checks this from inside the containers.

### The three invariants (and where they're enforced)

1. **The agent never possesses the DB credential and has no route to the DB.** Compose passes DB
   settings only to `db` and `broker`. `agent/requirements.txt` has no DB driver. The network
   topology is described above. Tests A3 and A4 check all three.
2. **The broker is the sole enforcement point.** All access logic is in
   `broker/app/policy.py` + `broker/app/main.py`, driven by `policy/policy.yaml`. The agent and
   UI contain no allow/deny logic. The UI only picks which user is acting.
3. **Structured requests, never raw SQL.** The agent sends `{table, columns, filter, limit, action}`.
   The broker builds parameterised SQL (`broker/app/db.py`) only after every check passes.

### Request lifecycle

`POST /query` on the broker, evaluated in order. Default is deny:

1. **token**: HMAC signature valid and not expired (`broker/app/agent_token.py`)
2. **agent_scope**: table + columns + action ⊆ the agent's scope
3. **user_rights**: table + columns ⊆ the acting user's `may_read`
4. **allow**: build parameterised SQL, run it read-only on the broker's own connection

Each stage emits a `broker_check` event, then a `decision` event follows. Every outcome is
appended to the audit log (`/var/log/broker/audit.jsonl` in the broker, also on stdout).
Filter columns count as read columns, because filtering on `salary` is a way of reading it.

**The effective permission is the intersection: agent scope ∩ user rights.** That intersection
is the confused-deputy defence. The agent can never reach, on a user's behalf, something that
user couldn't reach themselves.

### Identity

* **Agent:** the agent proves who it is with a client secret. In return it gets a short-lived
  (15 min) HMAC-signed token, `{agent_id, scope_ref, iat, exp}`. The signing secret never
  leaves the broker. To mint an expired token for testing:
  `docker compose exec broker python -m app.agent_token --ttl -60`
* **User:** picking a user in the UI starts a session for that user. The agent service stamps
  the session's user into every request as `on_behalf_of`. The LLM never sees it as a tool
  parameter and can't change it (test B3 tries a prompt injection).

### Deliberate design choice: explicit denies

When a user lacks rights, the broker returns an explicit "not permitted" reason. That lets the
agent explain the refusal. It also **leaks the data's existence** (Bob learns there's an
`employees.salary` column). The stricter alternative is to answer as if the resource doesn't
exist. This choice is marked `DECISION` in `broker/app/main.py`.

### Event stream (everything on screen is real)

The broker publishes `broker_check` / `decision` events on `GET /events` (SSE). The agent
service subscribes, merges in its own `agent_plan`, `agent_thinking`, `request_sent`,
`result_returned` and `agent_message` events, and fans one ordered stream out to the browser
over `/ws`. The agent service relays broker events verbatim and **refuses to author** them
(test C2 compares the relayed stream with the broker's own). Each event carries a `source`
field (`broker` | `agent`) in addition to the specified envelope. The UI animates events
strictly in arrival order. Its only liberty is pacing: each step is held on screen briefly so
a person can follow it, and a "queued" badge shows when playback is behind live.

## The demo narrative (preset objectives)

1. **Summarise open tickets.** The clean, in-scope task. It works for every user.
2. **Compensation review.** This needs `employees.salary`. As **Alice** (HR) it's allowed. As
   **Bob** (support), the *identical* request is denied at `user_rights`. Same agent, same
   request, opposite outcome. This is the key demo.
3. **Most urgent ticket (over-reads).** This is **misuse within scope**, and the gap is left
   unsolved on purpose. See below.
4. *(extra)* **Tickets and salaries.** For Bob or Carol this is partly beyond their rights. The
   agent does the allowed part and explains the rest.

### Talking to the agent

The chat is a real conversation, and follow-ups in the same session see what came before.
When the broker refuses something, the agent tells you in plain, non-technical language,
addressed to you: what it couldn't get, why (your account lacks access, or the assistant itself
is never allowed it), what it *could* do, and a sensible next step. Ask "why?" or "what can you
show me instead?" and it answers from the conversation. It only uses technical terms (table,
column, rule) if you ask for them. The technical detail is always on the right, in the system
view and the audit log. The agent is never silent:
* an empty model reply is retried, and otherwise replaced by a plain summary of what was refused;
* a run that errors or exceeds `AGENT_RUN_TIMEOUT_SECONDS` (default 480) ends with an apology message;
* the UI shows what the agent is doing and for how long, and recovers the reply if the live connection dropped.

This tone lives in the system prompt in `agent/app/graph.py`.

Users: **Alice** (HR manager: tickets + employees incl. salary), **Bob** (support analyst:
tickets only), **Carol** (contractor: tickets without customer names, max 5 rows).

### The visible gap: misuse within scope

Objective 3 ("find the most urgent ticket by reviewing every ticket in full detail") makes
the agent read the whole tickets table, every in-scope column, to produce a one-ticket answer.
Every request is within agent scope and user rights, so **the broker allows all of it**. The
audit log makes the excess plain: request count, rows read, and columns read, against an
answer that needed one row.

Catching this isn't the broker's job. Access control answers "*may* this be read?", not "does
this task *need* it?". That's the target of the next step, a **behavioural-governance layer**
(task-scoped purpose binding, data-minimisation budgets, anomaly detection on access patterns).
It's included here so the gap is visible, not hidden.

### Other things deliberately left for later

* **Egress control.** The agent needs outbound internet for the LLM API, so today it can reach
  *anything* outbound, including services on the host machine. Constraining that is a
  separate, later governance concern.
* **Row-level security, masking, views.** Postgres was chosen so these can be added in the broker.
* **Real user authentication.** The UI asserts the user. In production the session would come
  from an IdP, and the broker would verify a user assertion too.

## Repo layout

```
docker-compose.yml   two networks, four services
.env.example         DB password, broker secret, agent client secret, LLM key placeholders
policy/policy.yaml   agent scopes + user rights. Single source of truth, mounted read-only into the broker.
db/init/             schema + seed (12 tickets, 12 employees)
broker/              FastAPI: /token, /query, /events (SSE), /audit
agent/               LangGraph agent (graph.py), broker client, UI backend (/api/*, /ws), scripted Phase A client
ui/                  React (Vite) app served by nginx, which proxies /api and /ws to the agent
tests/               acceptance tests for phases A–F
```
