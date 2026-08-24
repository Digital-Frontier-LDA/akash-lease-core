"""Funding gate — decide whether a slot can fund a deployment create.

⛔ WHY THIS EXISTS: TWO SAMPLES CANNOT DISTINGUISH A STEP FROM A SLOPE.

The gate this replaces sampled the on-chain allowance twice 60s apart and
linearly projected the difference 300s forward::

    projected = a2 - drop * (HORIZON / GAP)        # akash-runner.yml:486

That is unsound, because the allowance does not DRIFT — it moves in discrete
quanta of exactly one deployment's escrow deposit and is FLAT between steps.
Measured for ``akash1cklqag`` on 2026-08-24 (ACT)::

    20:21:15  6.29     20:25:02  1.29
    20:22:32  1.29     20:25:50  6.29
    20:23:40  1.29     20:26:11 11.29
    20:24:20  1.29     20:27:49  6.29
    20:25:02  1.29     20:30:49  1.29

Every difference is 0.00 or ±5.00 — never a rate. A 60s window that STRADDLES a
step reads "fell 5.00 ACT in 60s" and projects −3.71 ACT at +300s, so it
REFUSES. The identical window BETWEEN steps reads flat and ALLOWS. The verdict
therefore depends on sampling phase rather than on funding, and it fires hardest
when the queue is busy — exactly when provisioning matters. It blocked #1538,
#1540 and #1541.

⇒ This module counts DEPOSITS. It never fits a line and never extrapolates.
A 5.00 ACT drop between two samples means *one deployment was created*; the
current level already reflects it, so it is not evidence of a future fall.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from decimal import Decimal
from enum import Enum

__all__ = [
    "AllowanceQuantity",
    "AllowanceSample",
    "FundingDecision",
    "FundingPolicy",
    "FundingStatus",
    "evaluate_funding",
]

# One deployment's escrow deposit, in uACT. This is the QUANTUM the allowance
# moves in — not a threshold someone chose.
DEPOSIT_UACT = 5_000_000


class FundingStatus(str, Enum):
    """⛔ THREE outcomes, never two.

    ``UNDETERMINED`` exists because collapsing an unreadable instrument into a
    number is how false zeros are manufactured. On 2026-08-24 alone that
    produced three of them, including ``just-akash balance --json`` reporting
    ``active_deployments: 0`` while the chain showed 50. "We could not read it"
    and "it is zero" are different facts and must not share a value.
    """

    ABOVE_FLOOR = "above_floor"
    BELOW_FLOOR = "below_floor"
    UNDETERMINED = "undetermined"


class AllowanceQuantity(str, Enum):
    """WHICH quantity was read. They are different and only one gates a create.

    ⛔ ``console_deploy_credit`` does NOT gate a deployment create. It is a
    Console-side display quantity; a create is refused on the on-chain
    DepositAuthorization. Reporting one while meaning the other is why a day was
    spent investigating provider capacity for what was an allowance problem.

    ⚠ ``onchain_authz_spend_limits`` is the PLURAL key. The singular
    ``spend_limit`` is a ``uakt:0`` decoy that reads as "no funding" on an
    account that is fully funded in ``uact``.
    """

    ONCHAIN_SPEND_LIMITS = "onchain_authz_spend_limits"
    CONSOLE_DEPLOY_CREDIT = "console_deploy_credit"


@dataclass(frozen=True, slots=True)
class AllowanceSample:
    """One observation of one slot's allowance.

    ``amount_uact=None`` means UNREADABLE — the query failed, the key was
    absent, or the endpoint was unreachable. It must never be written as 0.
    """

    slot: str
    quantity: AllowanceQuantity
    amount_uact: int | None
    observed_at: float = 0.0

    def __post_init__(self) -> None:
        if not self.slot:
            raise ValueError("slot must not be empty")
        if self.amount_uact is not None:
            if isinstance(self.amount_uact, bool) or not isinstance(self.amount_uact, int):
                raise ValueError("amount_uact must be an int or None (None = unreadable)")
            if self.amount_uact < 0:
                raise ValueError("amount_uact must be non-negative")


@dataclass(frozen=True, slots=True)
class FundingPolicy:
    """How many deposits a create needs, and how big one is."""

    required_deposits: int = 1
    deposit_uact: int = DEPOSIT_UACT
    version: str = "funding-gate/v1"

    def __post_init__(self) -> None:
        if self.required_deposits < 1:
            raise ValueError("required_deposits must be >= 1")
        if self.deposit_uact <= 0:
            raise ValueError("deposit_uact must be positive")


@dataclass(frozen=True, slots=True)
class FundingDecision:
    """The verdict, and the evidence it rests on."""

    status: FundingStatus
    slot: str | None
    quantity: AllowanceQuantity | None
    headroom_deposits: int | None
    reason: str
    version: str = "funding-gate/v1"

    @property
    def allows_create(self) -> bool:
        """⛔ ONLY ``ABOVE_FLOOR`` allows. ``UNDETERMINED`` does not."""
        return self.status is FundingStatus.ABOVE_FLOOR


def _act(uact: int) -> str:
    return f"{Decimal(uact) / Decimal(1_000_000):.2f} ACT"


def evaluate_funding(
    samples: Iterable[AllowanceSample],
    policy: FundingPolicy | None = None,
) -> FundingDecision:
    """Decide from a QUANTISED model — count deposits, never fit a line.

    ⚠ MAX(SLOT), NEVER THE SUM. A create draws its deposit from ONE account.
    Summing slots would authorise a create that no single slot can fund, which
    is the same class of error as reading a projection off a step function:
    an aggregate that describes no actual thing.

    ⚠ Only ``ONCHAIN_SPEND_LIMITS`` can produce ``ABOVE_FLOOR``. A run that read
    only ``CONSOLE_DEPLOY_CREDIT`` did not measure the gating quantity, so the
    honest verdict is ``UNDETERMINED`` — not a pass off the wrong number.
    """
    policy = policy or FundingPolicy()
    samples = list(samples)
    if not samples:
        return FundingDecision(
            FundingStatus.UNDETERMINED,
            None,
            None,
            None,
            "no samples supplied — nothing was measured",
            policy.version,
        )

    gating = [s for s in samples if s.quantity is AllowanceQuantity.ONCHAIN_SPEND_LIMITS]
    if not gating:
        seen = ", ".join(sorted({s.quantity.value for s in samples}))
        return FundingDecision(
            FundingStatus.UNDETERMINED,
            None,
            None,
            None,
            f"no on-chain authz spend_limits sample; only read [{seen}], which does not "
            f"gate a create — refusing to answer off the wrong quantity",
            policy.version,
        )

    # Latest READABLE observation per slot. Not the max over time: a stale high
    # reading is not evidence about now. Not a projection: the current level
    # already reflects every step that has happened.
    latest: dict[str, AllowanceSample] = {}
    for s in gating:
        if s.amount_uact is None:
            continue
        prev = latest.get(s.slot)
        if prev is None or s.observed_at >= prev.observed_at:
            latest[s.slot] = s

    if not latest:
        return FundingDecision(
            FundingStatus.UNDETERMINED,
            None,
            AllowanceQuantity.ONCHAIN_SPEND_LIMITS,
            None,
            f"all {len(gating)} on-chain sample(s) were UNREADABLE — cannot determine "
            f"funding. This is not zero.",
            policy.version,
        )

    # ⭐ THE QUANTISED DECISION: how many whole deposits fit, per slot.
    best_slot, best = max(
        ((slot, s) for slot, s in latest.items()),
        key=lambda kv: kv[1].amount_uact,  # type: ignore[index]
    )
    headroom = int(best.amount_uact) // policy.deposit_uact  # type: ignore[arg-type]

    if headroom >= policy.required_deposits:
        return FundingDecision(
            FundingStatus.ABOVE_FLOOR,
            best_slot,
            AllowanceQuantity.ONCHAIN_SPEND_LIMITS,
            headroom,
            f"{best_slot} holds {_act(int(best.amount_uact))} = {headroom} whole deposit(s) "
            f"of {_act(policy.deposit_uact)}; need {policy.required_deposits}",
            policy.version,
        )
    return FundingDecision(
        FundingStatus.BELOW_FLOOR,
        best_slot,
        AllowanceQuantity.ONCHAIN_SPEND_LIMITS,
        headroom,
        f"best slot {best_slot} holds {_act(int(best.amount_uact))} = {headroom} whole "
        f"deposit(s); need {policy.required_deposits}. Escrow is RECOVERABLE — closing a "
        f"deployment returns its deposit minus rent consumed.",
        policy.version,
    )


def step_deltas(
    samples: Sequence[AllowanceSample], policy: FundingPolicy | None = None
) -> list[int]:
    """Diagnostics only: consecutive changes expressed in whole deposits.

    ⚠ NEVER feed this to the decision. It exists so an operator can SEE that the
    series moves in quanta (…, 0, 0, -1, +1, +1, -1, …) rather than drifting —
    the evidence that a linear projection was the wrong model. Using it to
    forecast would reintroduce the defect this module removes.
    """
    policy = policy or FundingPolicy()
    readable = [s for s in samples if s.amount_uact is not None]
    return [
        (int(b.amount_uact) - int(a.amount_uact)) // policy.deposit_uact  # type: ignore[arg-type]
        # strict=False is REQUIRED, not a default: the two arguments are the same
        # list offset by one, so their lengths are n and n-1 BY DESIGN. strict=True
        # would raise on every non-empty input.
        for a, b in zip(readable, readable[1:], strict=False)
    ]
