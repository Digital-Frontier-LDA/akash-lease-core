"""SANS-I/O lease recovery policy for a struck lease.

This is the shared decision table for provisioners, closers, sweepers, and
future reapers.  It deliberately does not read a clock, query a transport, or
perform a retry.  Those concerns remain in the caller.

The load-bearing invariant is ``STRUCK + UNREADABLE``: a paid lease whose
readiness instrument cannot be read may be waited on or reported as a typed
failure, but it must never be abandoned or rerolled.  ``UNREADABLE`` is not
``FALSE`` and is not zero.  Returning a typed decision keeps each I/O adapter
from re-deriving this rule (the three independent funding re-derivations are
the precedent this module is intended to prevent).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

__all__ = [
    "LeaseRecoveryAction",
    "LeaseRecoveryDecision",
    "LeaseState",
    "QuotaState",
    "ReadinessEvidence",
    "evaluate_lease_recovery",
]


class LeaseState(str, Enum):
    """Whether an escrow-bearing lease exists."""

    NOT_STRUCK = "not_struck"
    STRUCK = "struck"
    CLOSED = "closed"


class ReadinessEvidence(str, Enum):
    """The latest runner/readiness observation.

    ``UNREADABLE`` means the instrument could not answer.  It is never a
    negative readiness result and never authorises a reroll.
    """

    READY = "ready"
    NOT_READY = "not_ready"
    UNREADABLE = "unreadable"


class QuotaState(str, Enum):
    """Availability of the read/API budget used by the caller."""

    AVAILABLE = "available"
    EXHAUSTED = "exhausted"
    UNREADABLE = "unreadable"


class LeaseRecoveryAction(str, Enum):
    """Recommended next action for the caller."""

    COMPLETE = "complete"
    WAIT = "wait"
    FAIL_LOUDLY = "fail_loudly"
    REROLL = "reroll"
    ALREADY_CLOSED = "already_closed"


@dataclass(frozen=True, slots=True)
class LeaseRecoveryDecision:
    """Typed policy result; no action is performed by this module."""

    lease_state: LeaseState
    readiness: ReadinessEvidence
    quota: QuotaState
    action: LeaseRecoveryAction
    permitted_actions: frozenset[LeaseRecoveryAction]
    reason: str

    @property
    def may_abandon(self) -> bool:
        """Whether a caller may abandon the lease under this decision."""

        return LeaseRecoveryAction.REROLL in self.permitted_actions

    @property
    def may_reroll(self) -> bool:
        """Whether a caller may select a new bid."""

        return self.may_abandon


def _as_enum(value: Enum | str, enum_type: type[Enum], name: str) -> Enum:
    if isinstance(value, enum_type):
        return value
    try:
        return enum_type(value)
    except (TypeError, ValueError) as exc:
        expected = ", ".join(member.value for member in enum_type)
        raise ValueError(f"{name} must be one of [{expected}], got {value!r}") from exc


def evaluate_lease_recovery(
    lease_state: LeaseState | str,
    readiness_evidence: ReadinessEvidence | str,
    quota_state: QuotaState | str,
) -> LeaseRecoveryDecision:
    """Decide what a caller may do without touching the lease or an instrument.

    A struck lease is escrow-bearing.  If readiness is unreadable, the only
    safe outcomes are ``WAIT`` or an explicit caller-side failure; abandoning
    or rerolling is forbidden even when quota is exhausted.  Before a lease is
    struck, an unreadable readiness result may be retried only when the caller
    has a readable quota budget.  Closed leases are terminal.
    """

    lease = _as_enum(lease_state, LeaseState, "lease_state")
    readiness = _as_enum(readiness_evidence, ReadinessEvidence, "readiness_evidence")
    quota = _as_enum(quota_state, QuotaState, "quota_state")

    if lease is LeaseState.CLOSED:
        return LeaseRecoveryDecision(
            lease,
            readiness,
            quota,
            LeaseRecoveryAction.ALREADY_CLOSED,
            frozenset({LeaseRecoveryAction.ALREADY_CLOSED}),
            "lease is already terminal; do not retry or close again",
        )

    if lease is LeaseState.STRUCK:
        if readiness is ReadinessEvidence.READY:
            return LeaseRecoveryDecision(
                lease,
                readiness,
                quota,
                LeaseRecoveryAction.COMPLETE,
                frozenset({LeaseRecoveryAction.COMPLETE}),
                "struck lease is ready; continue the active lease",
            )
        if readiness is ReadinessEvidence.UNREADABLE:
            return LeaseRecoveryDecision(
                lease,
                readiness,
                quota,
                LeaseRecoveryAction.WAIT,
                frozenset({LeaseRecoveryAction.WAIT, LeaseRecoveryAction.FAIL_LOUDLY}),
                "struck lease is paid but readiness is unreadable; wait or fail loudly, "
                "never abandon/reroll",
            )
        return LeaseRecoveryDecision(
            lease,
            readiness,
            quota,
            LeaseRecoveryAction.WAIT,
            frozenset({LeaseRecoveryAction.WAIT, LeaseRecoveryAction.FAIL_LOUDLY}),
            "struck lease is not ready; wait or fail loudly, never abandon/reroll",
        )

    if readiness is ReadinessEvidence.READY:
        permitted = frozenset({LeaseRecoveryAction.COMPLETE})
        return LeaseRecoveryDecision(
            lease,
            readiness,
            quota,
            LeaseRecoveryAction.COMPLETE,
            permitted,
            "no lease is struck and readiness is already available",
        )

    if readiness is ReadinessEvidence.UNREADABLE and quota is QuotaState.AVAILABLE:
        permitted = frozenset({LeaseRecoveryAction.WAIT, LeaseRecoveryAction.REROLL})
        return LeaseRecoveryDecision(
            lease,
            readiness,
            quota,
            LeaseRecoveryAction.REROLL,
            permitted,
            "no lease is struck; unreadable readiness may be retried while quota is available",
        )

    if quota is QuotaState.AVAILABLE:
        permitted = frozenset({LeaseRecoveryAction.WAIT, LeaseRecoveryAction.REROLL})
        return LeaseRecoveryDecision(
            lease,
            readiness,
            quota,
            LeaseRecoveryAction.REROLL,
            permitted,
            "no lease is struck; a negative readiness result may be retried",
        )

    permitted = frozenset({LeaseRecoveryAction.FAIL_LOUDLY})
    return LeaseRecoveryDecision(
        lease,
        readiness,
        quota,
        LeaseRecoveryAction.FAIL_LOUDLY,
        permitted,
        "no lease is struck but quota is not readable/available; fail loudly before bidding",
    )
