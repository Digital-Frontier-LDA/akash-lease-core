from __future__ import annotations

import pytest

from akash_lease_core.lease_recovery import (
    LeaseRecoveryAction,
    LeaseState,
    QuotaState,
    ReadinessEvidence,
    evaluate_lease_recovery,
)


def test_struck_unreadable_never_allows_abandon_or_reroll():
    decision = evaluate_lease_recovery(
        LeaseState.STRUCK, ReadinessEvidence.UNREADABLE, QuotaState.EXHAUSTED
    )

    assert decision.action is LeaseRecoveryAction.WAIT
    assert decision.may_abandon is False
    assert decision.may_reroll is False
    assert LeaseRecoveryAction.FAIL_LOUDLY in decision.permitted_actions


def test_struck_unreadable_with_readable_quota_still_cannot_reroll():
    decision = evaluate_lease_recovery("struck", "unreadable", "available")

    assert decision.action is LeaseRecoveryAction.WAIT
    assert decision.may_reroll is False


def test_struck_ready_completes_without_destructive_action():
    decision = evaluate_lease_recovery("struck", "ready", "available")

    assert decision.action is LeaseRecoveryAction.COMPLETE
    assert decision.permitted_actions == frozenset({LeaseRecoveryAction.COMPLETE})


def test_unstruck_unreadable_can_reroll_only_with_quota():
    allowed = evaluate_lease_recovery("not_struck", "unreadable", "available")
    refused = evaluate_lease_recovery("not_struck", "unreadable", "unreadable")

    assert allowed.action is LeaseRecoveryAction.REROLL
    assert allowed.may_reroll is True
    assert refused.action is LeaseRecoveryAction.FAIL_LOUDLY
    assert refused.may_reroll is False


def test_closed_is_terminal():
    decision = evaluate_lease_recovery("closed", "not_ready", "exhausted")

    assert decision.action is LeaseRecoveryAction.ALREADY_CLOSED
    assert decision.may_reroll is False


@pytest.mark.parametrize(
    ("field", "value"),
    [("lease_state", "bogus"), ("readiness_evidence", "bogus"), ("quota_state", "bogus")],
)
def test_unknown_state_is_rejected(field, value):
    values = {"lease_state": "struck", "readiness_evidence": "ready", "quota_state": "available"}
    values[field] = value

    with pytest.raises(ValueError, match=field):
        evaluate_lease_recovery(**values)
