"""Provider qualification — is this provider fit to carry a REQUIRED check?

consultant5 item 4 ("Separate PR correctness from provider-fleet qualification") asks for
a machine-readable eligibility set maintained by a rolling window, so required PR smoke
draws only from qualified providers and a bad draw is reported as
``provider-fleet-unavailable`` rather than as a product regression.

This module is the POLICY half. It is sans-I/O like the rest of the package: the caller
supplies observations and a clock reading; nothing here queries a chain or a provider.

WHY THIS EXISTS AT ALL — measured 2026-08-26
--------------------------------------------
On two runner orders whose provisioning jobs were failing at that moment, the chain said:

    bids=10  ours=[Lisbon, Helsinki]  ->  lease ACTIVE on Lisbon, 24 uact
    bids= 9  ours=[Lisbon, Helsinki]  ->  lease ACTIVE on Lisbon, 24 uact

Sofia — the ONLY provider tagged ``runner_host: true``, and therefore the one the runner
candidate ordering prefers — did not bid on either. Nothing recorded that. The fleet's
own registry carried a hand-written note claiming two OTHER providers were denied, which
had been false for long enough that people quoted it as a constraint. A rolling window
over typed outcomes is what replaces that note.

FOUR REFUSALS, EACH ENCODED RATHER THAN COMMENTED
-------------------------------------------------
1. **Too few observations is NOT qualified.** ``INSUFFICIENT_DATA`` is its own verdict.
   A provider nobody has measured is not thereby healthy, and a fleet that starts empty
   would otherwise qualify every provider in it by default.

2. **An UNCLASSIFIED outcome is not a pass.** A run that failed for an unknown reason
   counts against nothing and *for* nothing — it lowers confidence instead of raising
   the success rate. Folding "we could not tell" into "success" is how a provider that
   breaks in a new way stays qualified.

3. **A quarantine has a MINIMUM DURATION.** Without it a provider that fails, then gets
   one lucky success, re-enters the required path immediately and fails the next PR —
   flapping, with each flap costing a developer a red required check.

4. **Restoration needs MORE evidence than qualification.** Coming back from quarantine
   requires a strictly higher success rate than staying qualified does, because the prior
   is now against the provider. Symmetric thresholds make quarantine a coin-flip.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

__all__ = [
    "DEFAULT_POLICY",
    "Outcome",
    "ProviderObservation",
    "QualificationPolicy",
    "QualificationStatus",
    "QualificationVerdict",
    "evaluate_provider",
    "qualified_set",
]


class Outcome(str, Enum):
    """What one observation says about a provider.

    ⛔ UNCLASSIFIED is deliberately not a failure AND not a success. It is evidence that
    something happened and we could not attribute it — which must lower confidence
    without pretending to know the direction.
    """

    SUCCESS = "success"
    FAILURE = "failure"
    UNCLASSIFIED = "unclassified"


class QualificationStatus(str, Enum):
    QUALIFIED = "qualified"
    QUARANTINED = "quarantined"
    INSUFFICIENT_DATA = "insufficient_data"


@dataclass(frozen=True, slots=True)
class ProviderObservation:
    """One recorded outcome for one provider.

    ``at`` is a caller-supplied monotonic reading (seconds). This module never reads a
    clock — a policy that samples wall time cannot be tested deterministically, and every
    consumer already has a timestamp on hand.
    """

    provider: str
    outcome: Outcome
    at: float
    detail: str = ""


@dataclass(frozen=True, slots=True)
class QualificationPolicy:
    """What "fit for a required check" means. Every threshold is named, not implied."""

    window_seconds: float = 7 * 24 * 3600.0
    min_observations: int = 5
    # Stay qualified at or above this success rate.
    quarantine_below_rate: float = 0.80
    # Come BACK only at or above this one — deliberately higher (refusal 4).
    restore_at_or_above_rate: float = 0.90
    min_quarantine_seconds: float = 6 * 3600.0
    version: str = "provider-qualification/v1"

    def __post_init__(self) -> None:
        """⛔ A POLICY THAT CANNOT BE SATISFIED MUST NOT CONSTRUCT.

        `min_observations=0` made the evidence gate unreachable and the rate a 0/0 —
        `ZeroDivisionError` from a CONFIGURATION value, surfacing at evaluation time far
        from the mistake. Every threshold is checked here, where the wrong number was
        written.
        """
        if self.min_observations < 1:
            raise ValueError(
                f"min_observations must be >= 1, got {self.min_observations}: a policy "
                "that requires no evidence cannot distinguish an unmeasured provider "
                "from a healthy one"
            )
        if self.window_seconds <= 0:
            raise ValueError(f"window_seconds must be > 0, got {self.window_seconds}")
        if self.min_quarantine_seconds < 0:
            raise ValueError(
                f"min_quarantine_seconds must be >= 0, got {self.min_quarantine_seconds}"
            )
        for name, value in (
            ("quarantine_below_rate", self.quarantine_below_rate),
            ("restore_at_or_above_rate", self.restore_at_or_above_rate),
        ):
            if not 0.0 <= value <= 1.0:
                raise ValueError(f"{name} must be within [0, 1], got {value}")
        # ⚠ Refusal 4 is a PROPERTY OF THE POLICY, not only of the code path: a
        #   restoration bar below the qualification floor would make quarantine easier to
        #   leave than to avoid.
        if self.restore_at_or_above_rate < self.quarantine_below_rate:
            raise ValueError(
                f"restore_at_or_above_rate ({self.restore_at_or_above_rate}) must be >= "
                f"quarantine_below_rate ({self.quarantine_below_rate}): restoration must "
                "not be easier than staying qualified"
            )


DEFAULT_POLICY = QualificationPolicy()


@dataclass(frozen=True, slots=True)
class QualificationVerdict:
    provider: str
    status: QualificationStatus
    reason: str
    considered: int = 0
    successes: int = 0
    failures: int = 0
    unclassified: int = 0
    success_rate: float | None = None
    quarantined_until: float | None = None


def _in_window(obs: ProviderObservation, now: float, policy: QualificationPolicy) -> bool:
    # A reading from the future is not evidence about the past; treat it as out of window
    # rather than silently extending the window to meet it.
    return 0.0 <= (now - obs.at) <= policy.window_seconds


def evaluate_provider(
    provider: str,
    observations: list[ProviderObservation],
    now: float,
    policy: QualificationPolicy | None = None,
    quarantined_since: float | None = None,
) -> QualificationVerdict:
    """Classify one provider from its observations.

    ``quarantined_since`` is the caller's record of when this provider entered
    quarantine, or None. It is an INPUT because the minimum-duration rule cannot be
    derived from outcomes alone — the state has to be remembered somewhere, and this
    module has no storage.
    """
    pol = policy or DEFAULT_POLICY
    mine = [o for o in observations if o.provider == provider and _in_window(o, now, pol)]
    succ = sum(1 for o in mine if o.outcome is Outcome.SUCCESS)
    fail = sum(1 for o in mine if o.outcome is Outcome.FAILURE)
    unc = sum(1 for o in mine if o.outcome is Outcome.UNCLASSIFIED)

    # ⛔ REFUSAL 3 IS CHECKED FIRST, BECAUSE REFUSAL 1 WOULD OTHERWISE SHORT-CIRCUIT IT.
    #   If `window_seconds` is shorter than `min_quarantine_seconds`, every observation
    #   can age out of the window while the provider is still SERVING its quarantine.
    #   The evidence gate below then answered INSUFFICIENT_DATA — reporting an actively
    #   quarantined provider as merely unmeasured, which is the one reading that invites
    #   a caller to send it work "to gather data".
    #
    # ⚠ `success_rate` is None here, never 0.0: with no evidence in the window a 0% would
    #   be a measurement nobody took.
    if quarantined_since is not None and (now - quarantined_since) < pol.min_quarantine_seconds:
        return QualificationVerdict(
            provider=provider,
            status=QualificationStatus.QUARANTINED,
            reason=(
                f"held {now - quarantined_since:.0f}s of a "
                f"{pol.min_quarantine_seconds:.0f}s minimum; a provider that flaps back "
                "in costs a red required check per flap."
            ),
            considered=len(mine),
            successes=succ,
            failures=fail,
            unclassified=unc,
            success_rate=(succ / len(mine)) if mine else None,
            quarantined_until=quarantined_since + pol.min_quarantine_seconds,
        )

    # ⛔ REFUSAL 1. Not measured is not healthy.
    if len(mine) < pol.min_observations:
        return QualificationVerdict(
            provider=provider,
            status=QualificationStatus.INSUFFICIENT_DATA,
            reason=(
                f"{len(mine)} observation(s) in the last {pol.window_seconds:.0f}s; "
                f"{pol.min_observations} required. Not measured is not healthy."
            ),
            considered=len(mine),
            successes=succ,
            failures=fail,
            unclassified=unc,
        )

    # ⛔ REFUSAL 2. UNCLASSIFIED sits in the denominator and in neither numerator, so it
    #   lowers the rate without being counted as a failure.
    rate = succ / len(mine)

    if quarantined_since is not None:
        # The minimum-duration case already returned above, before the evidence gate.
        # ⛔ REFUSAL 4. Restoration needs a strictly higher bar than staying qualified.
        if rate < pol.restore_at_or_above_rate:
            return QualificationVerdict(
                provider=provider,
                status=QualificationStatus.QUARANTINED,
                reason=(
                    f"success rate {rate:.0%} below the {pol.restore_at_or_above_rate:.0%} "
                    "RESTORATION bar (higher than the qualification bar on purpose)"
                ),
                considered=len(mine),
                successes=succ,
                failures=fail,
                unclassified=unc,
                success_rate=rate,
            )
        return QualificationVerdict(
            provider=provider,
            status=QualificationStatus.QUALIFIED,
            reason=(
                f"restored: {rate:.0%} >= {pol.restore_at_or_above_rate:.0%} "
                "after serving quarantine"
            ),
            considered=len(mine),
            successes=succ,
            failures=fail,
            unclassified=unc,
            success_rate=rate,
        )

    if rate < pol.quarantine_below_rate:
        return QualificationVerdict(
            provider=provider,
            status=QualificationStatus.QUARANTINED,
            reason=f"success rate {rate:.0%} below the {pol.quarantine_below_rate:.0%} floor",
            considered=len(mine),
            successes=succ,
            failures=fail,
            unclassified=unc,
            success_rate=rate,
            quarantined_until=now + pol.min_quarantine_seconds,
        )
    return QualificationVerdict(
        provider=provider,
        status=QualificationStatus.QUALIFIED,
        reason=f"success rate {rate:.0%} at or above the {pol.quarantine_below_rate:.0%} floor",
        considered=len(mine),
        successes=succ,
        failures=fail,
        unclassified=unc,
        success_rate=rate,
    )


def qualified_set(
    providers: list[str],
    observations: list[ProviderObservation],
    now: float,
    policy: QualificationPolicy | None = None,
    quarantined_since: dict[str, float] | None = None,
) -> tuple[list[str], dict[str, QualificationVerdict]]:
    """The eligibility set required PR smoke may draw from, plus every verdict.

    ⛔ RETURNS THE VERDICTS TOO, ALWAYS. A caller handed only the qualified list cannot
    distinguish "the fleet is unhealthy" from "nobody has measured it yet" — and item 4's
    whole point is that an empty set must be reported as `provider-fleet-unavailable`
    rather than as a product regression. The reason has to travel with the answer.
    """
    since = quarantined_since or {}
    verdicts = {
        p: evaluate_provider(p, observations, now, policy, since.get(p)) for p in providers
    }
    return (
        [p for p in providers if verdicts[p].status is QualificationStatus.QUALIFIED],
        verdicts,
    )
