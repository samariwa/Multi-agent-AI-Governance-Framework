"""Policy evaluation. This is the ONLY place in the system where access is decided.

Effective permission = agent scope ∩ acting user's rights. That intersection is
the confused-deputy defence: the agent can never reach, on a user's behalf,
something that user could not reach themselves. Default is DENY.
"""

import os
from dataclasses import dataclass

import yaml


@dataclass
class Verdict:
    allowed: bool
    rule_fired: str | None = None
    reason: str | None = None


class Policy:
    def __init__(self, path: str):
        with open(path) as f:
            doc = yaml.safe_load(f) or {}
        self.agents: dict = doc.get("agents") or {}
        self.users: dict = doc.get("users") or {}
        limits = doc.get("limits") or {}
        self.global_max_rows: int = int(limits.get("max_rows", 100))
        self.max_delegation_depth: int = int(limits.get("max_delegation_depth", 2))
        self.delegated_token_ttl: int = int(limits.get("delegated_token_ttl_seconds", 300))

    def is_delegated_only(self, agent_id: str) -> bool:
        return bool((self.agents.get(agent_id) or {}).get("delegated_only"))

    def check_delegation(self, chain: list[str], to_agent: str) -> Verdict:
        """May the last agent in `chain` hand work to `to_agent`?"""
        frm = chain[-1]
        if to_agent not in self.agents:
            return Verdict(False, "delegation", f"no such agent '{to_agent}'")
        if to_agent in chain:
            return Verdict(False, "delegation", f"delegation cycle: '{to_agent}' is already in the chain {' → '.join(chain)}")
        if len(chain) + 1 > self.max_delegation_depth:
            return Verdict(False, "delegation", f"'{frm}' may not delegate further (max chain length {self.max_delegation_depth})")
        allowed = (self.agents.get(frm) or {}).get("may_delegate_to") or []
        if to_agent not in allowed:
            return Verdict(False, "delegation", f"agent '{frm}' not permitted to delegate to '{to_agent}'")
        return Verdict(True)

    def check_delegated_access(self, chain: list[str], bound_user: str | None, user: str,
                               action: str, table: str, columns: list[str]) -> Verdict:
        """For a delegated token: every link must still be permitted (policy is
        re-read at use, not just at handoff), the user must be the one the chain
        was created for, and the request must fit every delegator's ceiling."""
        for i in range(1, len(chain)):
            v = self.check_delegation(chain[:i], chain[i])
            if not v.allowed:
                return v
        if bound_user != user:
            return Verdict(False, "delegation", f"delegated token is bound to user '{bound_user}', not '{user}'")
        for ancestor in chain[:-1]:
            ceiling = (self.agents.get(ancestor) or {}).get("delegable") or {}
            if action not in (ceiling.get("actions") or []):
                return Verdict(False, "delegation", f"'{ancestor}' cannot delegate {action} access")
            allowed_cols = (ceiling.get("tables") or {}).get(table)
            if allowed_cols is None:
                return Verdict(False, "delegation", f"'{ancestor}' cannot delegate access to table '{table}'")
            missing = [c for c in columns if c not in allowed_cols]
            if missing:
                return Verdict(False, "delegation", f"'{ancestor}' cannot delegate access to {', '.join(f'{table}.{c}' for c in missing)}")
        return Verdict(True)

    def check_agent_scope(self, scope_ref: str, action: str, table: str, columns: list[str]) -> Verdict:
        agent = self.agents.get(scope_ref)
        if agent is None:
            return Verdict(False, "agent_scope", f"agent '{scope_ref}' has no scope defined")
        if action not in (agent.get("actions") or []):
            return Verdict(False, "agent_scope", f"agent '{scope_ref}' not permitted to {action} (scope allows: {', '.join(agent.get('actions') or []) or 'nothing'})")
        allowed_cols = (agent.get("tables") or {}).get(table)
        if allowed_cols is None:
            return Verdict(False, "agent_scope", f"agent '{scope_ref}' not permitted to access table '{table}'")
        missing = [c for c in columns if c not in allowed_cols]
        if missing:
            return Verdict(False, "agent_scope", f"agent '{scope_ref}' not permitted to {action} {', '.join(f'{table}.{c}' for c in missing)}")
        return Verdict(True)

    def check_user_rights(self, user: str, action: str, table: str, columns: list[str]) -> Verdict:
        rights = self.users.get(user)
        if rights is None:
            return Verdict(False, "user_rights", f"unknown user '{user}'")
        if action != "read":
            # Users are only ever granted reads in this demo (may_read).
            return Verdict(False, "user_rights", f"user '{user}' not permitted to {action}")
        allowed_cols = (rights.get("may_read") or {}).get(table)
        if allowed_cols is None:
            return Verdict(False, "user_rights", f"user '{user}' not permitted to read table '{table}'")
        missing = [c for c in columns if c not in allowed_cols]
        if missing:
            return Verdict(False, "user_rights", f"user '{user}' not permitted to read {', '.join(f'{table}.{c}' for c in missing)}")
        return Verdict(True)

    def row_limit(self, user: str, requested: int | None) -> int:
        caps = [self.global_max_rows]
        user_cap = (self.users.get(user) or {}).get("max_rows")
        if user_cap is not None:
            caps.append(int(user_cap))
        if requested is not None and requested > 0:
            caps.append(requested)
        return min(caps)


_policy: Policy | None = None


def get_policy() -> Policy:
    global _policy
    if _policy is None:
        _policy = Policy(os.environ.get("POLICY_PATH", "/policy/policy.yaml"))
    return _policy
