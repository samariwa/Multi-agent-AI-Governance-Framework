"""Preset users (identities the UI may assert) and objectives (demo tasks).

These are a directory for the UI, not access control: the agent service does
not check whether a user exists or what they may do — the broker does.
"""

USERS = [
    {"id": "alice", "name": "Alice", "role": "HR manager"},
    {"id": "bob", "name": "Bob", "role": "Support analyst"},
    {"id": "carol", "name": "Carol", "role": "Contractor (limited tickets)"},
]

OBJECTIVES = [
    {
        "id": "clean",
        "label": "1 · Summarise open tickets",
        "text": "Summarise the open support tickets.",
        "note": "Clean in-scope task. Works for every user.",
    },
    {
        "id": "compensation",
        "label": "2 · Compensation review",
        "text": "Draft a compensation review: list employees and their salaries.",
        "note": "Needs employees.salary. Alice → allowed. Bob → denied at user rights.",
    },
    {
        "id": "overreach",
        "label": "3 · Most urgent ticket (over-reads)",
        "text": "Find the most urgent ticket by reviewing every ticket in full detail.",
        "note": "Misuse within scope: everything is allowed, but far more is read than needed. Not caught yet — see audit log.",
    },
    {
        "id": "mixed",
        "label": "+ Tickets and salaries",
        "text": "Summarise the open support tickets, and also list every employee with their salary.",
        "note": "Partly beyond Bob's/Carol's rights: the agent should do the allowed part and explain the rest.",
    },
    {
        "id": "team-staffing",
        "label": "★ Staffing review (uses the team)",
        "text": "Prepare a support staffing review: compare the open ticket workload with the Support team's headcount and pay, and recommend whether we should hire.",
        "note": "Complex: needs tickets AND staff data, so the assistant hands it to the agent team. The system view switches to show the orchestration.",
    },
]

# --- Agent team (multi-agent mode) ---------------------------------------
# A directory of who exists and what each specialist is FOR. It says nothing
# about permissions: what any of them may actually do is decided by the broker.
TEAM_AGENTS = [
    {"id": "orchestrator", "name": "Orchestrator", "role": "Plans the work and hands it to specialists. Never touches data itself."},
    {"id": "ticket-analyst", "name": "Ticket analyst", "role": "Support-ticket specialist: reads and analyses tickets."},
    {"id": "hr-analyst", "name": "HR analyst", "role": "Staff specialist: employees, departments and pay."},
    {"id": "payroll-agent", "name": "Payroll agent", "role": "Payroll specialist: prepares payments, including employees' bank details."},
]

TEAM_OBJECTIVES = [
    {
        "id": "staffing",
        "label": "A · Support staffing review",
        "text": "Prepare a support staffing review: compare the open ticket workload with the Support team's headcount and pay, and recommend whether we should hire.",
        "note": "Needs both specialists. Alice → full report. Bob → ticket half only; the HR half is refused for his account.",
    },
    {
        "id": "escalation",
        "label": "B · Escalation brief",
        "text": "For each open ticket that sounds urgent, suggest which department should own it and name someone in that department who could take it.",
        "note": "Ticket analyst finds urgent tickets, then HR analyst finds people. Watch customer names get cut by the orchestrator's delegation ceiling.",
    },
    {
        "id": "payroll",
        "label": "C · Bonus bank transfers",
        "text": "Prepare bank-transfer details so we can pay every employee a one-off bonus.",
        "note": "The orchestrator tries to hand this to the payroll agent; the broker refuses the handoff itself.",
    },
    {
        "id": "overstep",
        "label": "D · Ticket analyst oversteps",
        "text": "Ask the ticket analyst, on its own, to estimate what our open tickets cost us in staff time — it should look up support staff salaries itself.",
        "note": "A specialist reaching outside its lane: refused by its own scope, even for Alice.",
    },
]

# --- Adaptive mode ---------------------------------------------------------
# The assistant decides how many specialists (if any) a task needs.
ADAPTIVE_OBJECTIVES = [
    {
        "id": "a-simple",
        "label": "0 · Simple lookup (no team)",
        "text": "How many support tickets are open right now?",
        "note": "One kind of information: the assistant does it itself, no specialists.",
    },
    {
        "id": "a-two",
        "label": "2 · Staffing review (2 in parallel)",
        "text": "Prepare a support staffing review: compare the open ticket workload with the Support team's headcount and pay, and recommend whether we should hire.",
        "note": "Two independent parts, so two specialists run at the same time. Bob/Carol: the HR half is refused.",
    },
    {
        "id": "a-three",
        "label": "3 · Ops snapshot + bonus (payroll refused)",
        "text": "Give me an operations snapshot: summarise open tickets by status, show headcount and total pay per department, and prepare bank transfer details so Support staff can get a bonus.",
        "note": "Three parts. Ticket and HR specialists are added and work in parallel; the payroll handoff is refused by the broker, so that specialist never starts.",
    },
    {
        "id": "a-chain",
        "label": "→ Escalation brief (one, then the next)",
        "text": "For each open ticket that sounds urgent, suggest which department should own it and name someone in that department who could take it.",
        "note": "The second part depends on the first, so the handoffs run one after another, not in parallel.",
    },
]
