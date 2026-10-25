-- Schema for the demo database. Only the broker ever connects to this.
--
-- Some columns (tickets.internal_notes, employees.bank_account) exist in the
-- database but are deliberately NOT in the agent's scope (policy/policy.yaml),
-- so an agent-scope denial can be demonstrated even for a fully privileged user.

CREATE TABLE tickets (
    id             SERIAL PRIMARY KEY,
    subject        TEXT        NOT NULL,
    status         TEXT        NOT NULL CHECK (status IN ('open', 'pending', 'closed')),
    created_at     TIMESTAMPTZ NOT NULL,
    customer_name  TEXT        NOT NULL,
    internal_notes TEXT
);

CREATE TABLE employees (
    id           SERIAL PRIMARY KEY,
    name         TEXT    NOT NULL,
    department   TEXT    NOT NULL,
    salary       INTEGER NOT NULL,
    bank_account TEXT    NOT NULL
);
