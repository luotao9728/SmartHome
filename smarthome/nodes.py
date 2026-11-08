"""Node authentication and provisioning.

Each microcontroller gets its own bearer token (``shd_...``).  The database
stores only a SHA-256 of it, so a stolen database backup cannot be replayed
against the hub.

Three ways to get a token onto a board:

1. **Enrolment window** (easiest): the admin presses "Allow new devices" in the
   UI, the board connects with ``enroll=<node_id>`` and receives a fresh token
   in the hello reply, which it persists in ``/config.json`` on its filesystem.
2. **Manual token**: the admin creates a token in the UI and copies it into the
   board's ``config.json``.
3. **Open mode**: ``auth.require_node_tokens = false`` for a trusted LAN while
   you are experimenting.

The hub tolerates a token being presented by the *same* node id only; a token
is bound to its node the first time it is used.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional

from . import security
from .store import Store

LOG = logging.getLogger(__name__)

ENROLL_TTL_S = 600.0


@dataclass
class AuthResult:
    ok: bool
    node_id: Optional[str] = None
    enroll: bool = False
    reason: str = ""
    new_token: Optional[str] = None


class NodeAuth:
    """Token store and enrolment window."""

    def __init__(self, store: Store, *, require_tokens: bool = True):
        self.store = store
        self.require_tokens = require_tokens
        self._enroll_until = 0.0

    # -- enrolment -------------------------------------------------------- #
    def open_enrollment(self, seconds: float = ENROLL_TTL_S) -> float:
        self._enroll_until = time.time() + max(5.0, min(float(seconds), 3600.0))
        LOG.info("enrolment window opened until %s", time.ctime(self._enroll_until))
        return self._enroll_until

    def close_enrollment(self) -> None:
        self._enroll_until = 0.0

    @property
    def enrollment_open(self) -> bool:
        return time.time() < self._enroll_until

    @property
    def enrollment_seconds_left(self) -> float:
        return max(0.0, self._enroll_until - time.time())

    # -- tokens ----------------------------------------------------------- #
    def issue_token(self, node_id: str) -> str:
        token = security.new_device_token()
        self.store.set_node_token_hash(node_id, security.hash_device_token(token))
        return token

    def revoke(self, node_id: str) -> None:
        self.store.set_node_token_hash(node_id, "")

    def has_token(self, node_id: str) -> bool:
        return bool(self.store.node_token_hash(node_id))

    # -- verification ----------------------------------------------------- #
    def authenticate(self, token: Optional[str], claimed_node: Optional[str]) -> AuthResult:
        if not token:
            if not self.require_tokens:
                return AuthResult(True, node_id=claimed_node)
            if claimed_node and self.enrollment_open:
                return AuthResult(True, node_id=claimed_node, enroll=True)
            if self.enrollment_open:
                return AuthResult(True, node_id=claimed_node, enroll=True)
            return AuthResult(False, reason="missing token")

        digest = security.hash_device_token(token)
        candidates = []
        if claimed_node:
            stored = self.store.node_token_hash(claimed_node)
            if stored:
                candidates.append((claimed_node, stored))
        else:
            # Token without a node id: find the owner (rare, but keeps manual
            # provisioning workable when a board forgets its node id).
            for row in self.store.list_nodes():
                if row.get("node_id") and self.store.node_token_hash(row["node_id"]):
                    candidates.append((row["node_id"], self.store.node_token_hash(row["node_id"])))

        for node_id, stored in candidates:
            if stored and security.safe_compare(digest, stored):
                return AuthResult(True, node_id=node_id)

        if claimed_node and self.enrollment_open and not self.store.node_token_hash(claimed_node):
            # Re-enrolling a node that never had a token is fine; a node that
            # already has one must present it.
            return AuthResult(True, node_id=claimed_node, enroll=True)
        return AuthResult(False, reason="invalid token")

    def token_for_new_node(self, node_id: str) -> str:
        return self.issue_token(node_id)

    def describe(self) -> Dict[str, Any]:
        return {
            "require_tokens": self.require_tokens,
            "enrollment_open": self.enrollment_open,
            "enrollment_seconds_left": round(self.enrollment_seconds_left, 1),
        }
