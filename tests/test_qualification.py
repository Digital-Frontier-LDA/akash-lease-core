"""Provider qualification: the four refusals, each tested as behaviour.

consultant5 item 4 asks for a machine-readable eligibility set so required PR smoke draws
only from qualified providers, and an empty set is reported as `provider-fleet-unavailable`
rather than as a product regression.

⛔ Grounded in a measurement, not a hypothetical. 2026-08-26, on two runner orders whose
provisioning jobs were failing: `bids=10 ours=[Lisbon, Helsinki]` → lease ACTIVE on Lisbon.
Sofia — the only `runner_host: true` provider, the one the candidate ordering PREFERS —
did not bid on either, and nothing recorded that.
"""

from __future__ import annotations

import pytest

from akash_lease_core.qualification import (
    DEFAULT_POLICY,
    Outcome,
    ProviderObservation,
    QualificationPolicy,
    QualificationStatus,
    evaluate_provider,
    qualified_set,
)

NOW = 1_000_000.0
P = "akash1sofia"


def _obs(n, outcome, provider=P, age=10.0):
    return [ProviderObservation(provider, outcome, NOW - age) for _ in range(n)]


class TestRefusal1NotMeasuredIsNotHealthy:
    def test_no_observations_is_insufficient_not_qualified(self):
        v = evaluate_provider(P, [], NOW)
        assert v.status is QualificationStatus.INSUFFICIENT_DATA
        assert v.status is not QualificationStatus.QUALIFIED

    def test_below_the_minimum_is_insufficient_even_if_all_succeeded(self):
        """⛔ A perfect 4-for-4 is still not enough evidence. Otherwise a fresh fleet
        qualifies everyone in it by default."""
        v = evaluate_provider(P, _obs(4, Outcome.SUCCESS), NOW)
        assert v.status is QualificationStatus.INSUFFICIENT_DATA
        assert v.successes == 4

    def test_at_the_minimum_it_becomes_judgeable(self):
        v = evaluate_provider(P, _obs(5, Outcome.SUCCESS), NOW)
        assert v.status is QualificationStatus.QUALIFIED


class TestRefusal2UnclassifiedIsNotAPass:
    def test_unclassified_lowers_the_rate_without_counting_as_failure(self):
        obs = _obs(5, Outcome.SUCCESS) + _obs(5, Outcome.UNCLASSIFIED)
        v = evaluate_provider(P, obs, NOW)
        assert v.unclassified == 5
        assert v.failures == 0
        assert v.success_rate == pytest.approx(0.5)
        assert v.status is QualificationStatus.QUARANTINED

    def test_all_unclassified_is_not_qualified(self):
        """⛔ Ten runs, none attributable. That is the least evidence of health, not the
        most — folding it into success is how a provider breaking in a NEW way stays in."""
        v = evaluate_provider(P, _obs(10, Outcome.UNCLASSIFIED), NOW)
        assert v.status is QualificationStatus.QUARANTINED
        assert v.success_rate == pytest.approx(0.0)


class TestRefusal3QuarantineHasAMinimumDuration:
    def test_a_lucky_success_does_not_end_quarantine_early(self):
        v = evaluate_provider(
            P,
            _obs(10, Outcome.SUCCESS),
            NOW,
            quarantined_since=NOW - 60.0,  # one minute in
        )
        assert v.status is QualificationStatus.QUARANTINED
        assert "minimum" in v.reason
        assert v.quarantined_until == pytest.approx(
            NOW - 60.0 + DEFAULT_POLICY.min_quarantine_seconds
        )

    def test_after_serving_the_minimum_a_clean_window_restores(self):
        v = evaluate_provider(
            P,
            _obs(10, Outcome.SUCCESS),
            NOW,
            quarantined_since=NOW - DEFAULT_POLICY.min_quarantine_seconds - 1,
        )
        assert v.status is QualificationStatus.QUALIFIED


class TestRefusal4RestorationIsHarderThanStaying:
    def test_a_rate_that_would_keep_you_in_does_not_get_you_back(self):
        """⛔ 85% stays qualified (floor 80%) but does NOT restore (bar 90%). Symmetric
        thresholds make quarantine a coin-flip."""
        obs = _obs(17, Outcome.SUCCESS) + _obs(3, Outcome.FAILURE)  # 85%
        staying = evaluate_provider(P, obs, NOW)
        assert staying.status is QualificationStatus.QUALIFIED

        returning = evaluate_provider(
            P, obs, NOW, quarantined_since=NOW - DEFAULT_POLICY.min_quarantine_seconds - 1
        )
        assert returning.status is QualificationStatus.QUARANTINED
        assert "RESTORATION" in returning.reason


class TestTheWindow:
    def test_observations_outside_the_window_are_not_counted(self):
        stale = [
            ProviderObservation(P, Outcome.SUCCESS, NOW - DEFAULT_POLICY.window_seconds - 1)
        ] * 10
        v = evaluate_provider(P, stale, NOW)
        assert v.status is QualificationStatus.INSUFFICIENT_DATA, (
            "a stale window qualified a provider"
        )

    def test_a_reading_from_the_future_is_not_evidence(self):
        """Clock skew must not extend the window to meet it."""
        future = [ProviderObservation(P, Outcome.SUCCESS, NOW + 500.0)] * 10
        v = evaluate_provider(P, future, NOW)
        assert v.status is QualificationStatus.INSUFFICIENT_DATA

    def test_other_providers_observations_are_not_borrowed(self):
        """⭐ The control. Without the provider filter every verdict is the fleet's
        average and no provider is ever individually judged."""
        others = [ProviderObservation("akash1lisbon", Outcome.SUCCESS, NOW - 10)] * 10
        v = evaluate_provider(P, others, NOW)
        assert v.status is QualificationStatus.INSUFFICIENT_DATA
        assert v.considered == 0


class TestTheEligibilitySet:
    def test_the_measured_case_sofia_quarantines_while_lisbon_qualifies(self):
        """The 2026-08-26 shape: Sofia not delivering, Lisbon winning leases."""
        obs = (
            [ProviderObservation("akash1sofia", Outcome.FAILURE, NOW - 10)] * 8
            + [ProviderObservation("akash1sofia", Outcome.SUCCESS, NOW - 10)] * 2
            + [ProviderObservation("akash1lisbon", Outcome.SUCCESS, NOW - 10)] * 10
        )
        eligible, verdicts = qualified_set(["akash1sofia", "akash1lisbon"], obs, NOW)
        assert eligible == ["akash1lisbon"]
        assert verdicts["akash1sofia"].status is QualificationStatus.QUARANTINED

    def test_an_empty_set_is_distinguishable_from_an_unmeasured_fleet(self):
        """⛔ ITEM 4'S WHOLE POINT. `provider-fleet-unavailable` must not be reported as a
        product regression — and 'nobody measured' must not read as 'all unhealthy'."""
        unmeasured, v1 = qualified_set(["a", "b"], [], NOW)
        unhealthy_obs = [ProviderObservation("a", Outcome.FAILURE, NOW - 10)] * 10
        unhealthy, v2 = qualified_set(["a"], unhealthy_obs, NOW)
        assert unmeasured == [] and unhealthy == []
        assert v1["a"].status is QualificationStatus.INSUFFICIENT_DATA
        assert v2["a"].status is QualificationStatus.QUARANTINED
        assert v1["a"].status is not v2["a"].status, (
            "an unmeasured fleet and an unhealthy one produce the same empty list; the "
            "verdicts must still tell them apart"
        )

    def test_verdicts_are_returned_for_every_provider_not_just_the_rejected(self):
        eligible, verdicts = qualified_set(["a", "b"], [], NOW)
        assert set(verdicts) == {"a", "b"}


def test_a_custom_policy_is_honoured_and_not_silently_defaulted():
    """⭐ Control: if the policy argument were ignored, every threshold test above would
    still pass by coincidence of the defaults."""
    strict = QualificationPolicy(min_observations=50)
    v = evaluate_provider(P, _obs(10, Outcome.SUCCESS), NOW, policy=strict)
    assert v.status is QualificationStatus.INSUFFICIENT_DATA


class TestReviewFindingsFromPR28:
    """Two real defects found in review, each reproduced before it was fixed."""

    def test_a_policy_requiring_no_evidence_cannot_be_constructed(self):
        """⛔ `min_observations=0` made the evidence gate unreachable and the rate a 0/0.
        The failure surfaced as ZeroDivisionError at EVALUATION time, far from the
        configuration line that caused it."""
        with pytest.raises(ValueError, match="min_observations"):
            QualificationPolicy(min_observations=0)

    @pytest.mark.parametrize(
        "kwargs, match",
        [
            ({"window_seconds": 0}, "window_seconds"),
            ({"min_quarantine_seconds": -1}, "min_quarantine_seconds"),
            ({"quarantine_below_rate": 1.5}, "quarantine_below_rate"),
            ({"restore_at_or_above_rate": -0.1}, "restore_at_or_above_rate"),
            # Refusal 4 as a property of the POLICY, not just of the code path.
            (
                {"quarantine_below_rate": 0.9, "restore_at_or_above_rate": 0.5},
                "not be easier than staying qualified",
            ),
        ],
    )
    def test_unsatisfiable_thresholds_are_rejected_at_construction(self, kwargs, match):
        with pytest.raises(ValueError, match=match):
            QualificationPolicy(**kwargs)

    def test_the_default_policy_is_still_constructible(self):
        """⭐ The control. Validation that rejects the defaults would pass every test
        above while making the module unusable."""
        assert QualificationPolicy().min_observations >= 1
        assert DEFAULT_POLICY.restore_at_or_above_rate >= DEFAULT_POLICY.quarantine_below_rate

    def test_an_active_quarantine_outranks_the_evidence_gate(self):
        """⛔ REFUSAL 3 WAS SHORT-CIRCUITED BY REFUSAL 1. With a window shorter than the
        quarantine minimum, every observation ages out while the provider is still
        SERVING its quarantine — and the evidence gate answered INSUFFICIENT_DATA,
        reporting an actively quarantined provider as merely unmeasured. That is the one
        reading that invites a caller to send it work "to gather data".
        """
        pol = QualificationPolicy(
            window_seconds=1.0, min_quarantine_seconds=3600.0, min_observations=5
        )
        stale = [ProviderObservation(P, Outcome.FAILURE, NOW - 600)] * 10
        v = evaluate_provider(P, stale, NOW, policy=pol, quarantined_since=NOW - 600)
        assert v.status is QualificationStatus.QUARANTINED
        assert v.status is not QualificationStatus.INSUFFICIENT_DATA

    def test_with_no_evidence_in_window_the_rate_is_none_not_zero(self):
        """A 0% would be a measurement nobody took."""
        pol = QualificationPolicy(
            window_seconds=1.0, min_quarantine_seconds=3600.0, min_observations=5
        )
        stale = [ProviderObservation(P, Outcome.FAILURE, NOW - 600)] * 10
        v = evaluate_provider(P, stale, NOW, policy=pol, quarantined_since=NOW - 600)
        assert v.considered == 0
        assert v.success_rate is None
