"""Funding gate — quantised, three-outcome, max(slot).

The series below is REAL: ``akash1cklqag`` on 2026-08-24, the day the two-sample
projection blocked provisioning on #1538, #1540 and #1541.
"""

from __future__ import annotations

import pytest

from akash_lease_core.funding import (
    DEPOSIT_UACT,
    AllowanceQuantity,
    AllowanceSample,
    FundingPolicy,
    FundingStatus,
    evaluate_funding,
    step_deltas,
)

ONCHAIN = AllowanceQuantity.ONCHAIN_SPEND_LIMITS
CONSOLE = AllowanceQuantity.CONSOLE_DEPLOY_CREDIT

# (hh:mm:ss as seconds-since-midnight, ACT) — measured, not synthesised.
MEASURED = [
    (73275, 6.29),
    (73352, 1.29),
    (73420, 1.29),
    (73460, 1.29),
    (73502, 1.29),
    (73550, 6.29),
    (73571, 11.29),
    (73669, 6.29),
    (73849, 1.29),
]
SERIES = [
    AllowanceSample("akash1cklqag", ONCHAIN, int(round(act * 1_000_000)), float(t))
    for t, act in MEASURED
]


def _old_two_sample_projection(
    a1: int, a2: int, gap_s: int, horizon_s: int, floor_uact: int
) -> bool:
    """The REPLACED logic, verbatim in shape — akash-runner.yml:486.

        projected = a2 - drop * (HORIZON / GAP)

    Returns True if it would ALLOW the create.
    """
    drop = max(0, a1 - a2)
    projected = a2 - (drop * horizon_s // gap_s) if drop > 0 else a2
    return projected >= floor_uact


# ─────────────────────────── the quantised model ───────────────────────────


def test_the_series_moves_in_whole_deposits_not_a_rate():
    """★ The premise. Every step is 0 or ±1 deposit — there is no slope to fit."""
    deltas = step_deltas(SERIES)
    assert deltas == [-1, 0, 0, 0, 1, 1, -1, -1], deltas
    assert all(d in (-1, 0, 1) for d in deltas), "a non-quantum step would refute the model"


def test_known_positive_above_floor():
    """6.29 ACT holds exactly one 5.00 deposit ⇒ ALLOW."""
    d = evaluate_funding([AllowanceSample("s", ONCHAIN, 6_290_000, 1.0)])
    assert d.status is FundingStatus.ABOVE_FLOOR
    assert d.headroom_deposits == 1
    assert d.allows_create is True


def test_known_negative_below_floor():
    """1.29 ACT holds zero whole deposits ⇒ REFUSE."""
    d = evaluate_funding([AllowanceSample("s", ONCHAIN, 1_290_000, 1.0)])
    assert d.status is FundingStatus.BELOW_FLOOR
    assert d.headroom_deposits == 0
    assert d.allows_create is False


# ────────────────────── ⭐ the mutation: old logic FAILS ──────────────────────


def test_mutation_the_old_projection_flips_on_sampling_phase_and_the_new_gate_does_not():
    """⭐ THE CONTROL. Two windows that END AT THE SAME FUNDED LEVEL — 6.29 ACT.

    One straddles a step, one does not. The account is in the SAME state at the
    end of both. The old projection disagrees with itself; the quantised gate
    does not. A control that passed on both models would be vacuous.

    ⚠ Note what this does NOT claim. At 1.29 ACT the old logic ALSO refuses —
    its floor comparison catches that — so there is no false PASS at a starved
    level. The defect is one-sided: a FALSE BLOCK of a funded account, and it
    fires only when a create happened inside the sampling window.
    """
    floor = DEPOSIT_UACT

    # Window A — STRADDLES a step: 11.29 → 6.29 (one create landed).  #1538/#1540/#1541.
    old_straddling = _old_two_sample_projection(11_290_000, 6_290_000, 98, 300, floor)
    # Window B — BETWEEN steps, same end level: 6.29 → 6.29.
    old_flat = _old_two_sample_projection(6_290_000, 6_290_000, 98, 300, floor)

    assert old_flat is True, "flat window at 6.29 ACT allows"
    assert old_straddling is False, "same end level, but a step in the window blocks"
    assert old_straddling != old_flat, (
        "the old verdict depends on sampling PHASE, not on funding — the account holds "
        "6.29 ACT in both cases"
    )

    # ⇒ The quantised gate reads the LEVEL and agrees with itself.
    new_straddling = evaluate_funding([SERIES[6], SERIES[7]])  # 11.29 → 6.29
    new_flat = evaluate_funding([AllowanceSample("akash1cklqag", ONCHAIN, 6_290_000, 99.0)])
    assert new_straddling.status is FundingStatus.ABOVE_FLOOR
    assert new_flat.status is FundingStatus.ABOVE_FLOOR
    assert new_straddling.status == new_flat.status, "phase must not change the verdict"


def test_mutation_old_logic_blocks_a_HEALTHY_account_across_a_step():
    """The #1538/#1540/#1541 failure: 11.29 ACT — two whole deposits — REFUSED."""
    a1, a2 = 11_290_000, 6_290_000  # 20:26:11 → 20:27:49, one create happened
    assert _old_two_sample_projection(a1, a2, 98, 300, DEPOSIT_UACT) is False, (
        "old logic projects 6.29 - 5.00*(300/98) < 5.00 and blocks"
    )
    d = evaluate_funding([SERIES[6], SERIES[7]])
    assert d.status is FundingStatus.ABOVE_FLOOR, "6.29 ACT funds one deposit — it is not broke"
    assert d.headroom_deposits == 1


# ─────────────────── three outcomes / quantity / max(slot) ───────────────────


def test_unreadable_is_undetermined_never_zero():
    d = evaluate_funding([AllowanceSample("s", ONCHAIN, None, 1.0)])
    assert d.status is FundingStatus.UNDETERMINED
    assert d.headroom_deposits is None
    assert d.allows_create is False, "undetermined must not allow a create"
    assert "not zero" in d.reason.lower()


def test_console_deploy_credit_alone_cannot_authorise_a_create():
    """⛔ Different quantity. Only on-chain spend_limits gates a create."""
    d = evaluate_funding([AllowanceSample("s", CONSOLE, 999_000_000, 1.0)])
    assert d.status is FundingStatus.UNDETERMINED
    assert "does not gate" in d.reason


def test_the_gate_reads_max_slot_never_the_sum():
    """Three slots of 2.00 ACT sum to 6.00 but no single one funds a 5.00 deposit."""
    s = [AllowanceSample(f"slot{i}", ONCHAIN, 2_000_000, 1.0) for i in range(3)]
    d = evaluate_funding(s)
    assert d.status is FundingStatus.BELOW_FLOOR, "summing slots would wrongly allow"
    assert d.headroom_deposits == 0


def test_max_slot_picks_the_funded_one():
    s = [
        AllowanceSample("empty", ONCHAIN, 1_290_000, 1.0),
        AllowanceSample("funded", ONCHAIN, 11_290_000, 1.0),
    ]
    d = evaluate_funding(s)
    assert d.status is FundingStatus.ABOVE_FLOOR
    assert d.slot == "funded"
    assert d.headroom_deposits == 2


def test_latest_readable_sample_wins_not_the_stale_high_one():
    s = [
        AllowanceSample("s", ONCHAIN, 11_290_000, 10.0),
        AllowanceSample("s", ONCHAIN, 1_290_000, 20.0),
    ]
    assert evaluate_funding(s).status is FundingStatus.BELOW_FLOOR


def test_a_newer_unreadable_sample_does_not_leave_a_stale_pass():
    """KP, load-bearing. The NEWEST observation for the slot is unreadable.

    The older readable reading was above the floor, but it is not evidence about
    now. Answering ABOVE_FLOOR here would be a false PASS at an account that may
    since have been drained — the one direction that costs money. "We could not
    ask" must resolve to UNDETERMINED, never to a stale yes.

    ⚠ This test previously asserted ABOVE_FLOOR and was GREEN. It was pinning the
    defect, not the contract.
    """
    s = [
        AllowanceSample("s", ONCHAIN, None, 30.0),
        AllowanceSample("s", ONCHAIN, 11_290_000, 20.0),
    ]
    d = evaluate_funding(s)
    assert d.status is FundingStatus.UNDETERMINED, (
        "a newer UNREADABLE sample must not be overridden by an older readable one"
    )
    assert d.allows_create is False


def test_an_unreadable_slot_does_not_veto_a_different_readable_slot():
    """KN, load-bearing. One slot unreadable must NOT collapse the whole decision.

    The gate reads max(SLOT). Slot "dark" cannot be read at all; slot "funded" is
    currently readable and above the floor. A create is legitimately fundable on
    "funded". Without this, the fix above would over-correct into refusing every
    evaluation in which any single slot happens to be unreadable.
    """
    s = [
        AllowanceSample("dark", ONCHAIN, 11_290_000, 10.0),
        AllowanceSample("dark", ONCHAIN, None, 30.0),
        AllowanceSample("funded", ONCHAIN, 11_290_000, 29.0),
    ]
    d = evaluate_funding(s)
    assert d.status is FundingStatus.ABOVE_FLOOR
    assert d.slot == "funded"


def test_an_older_unreadable_sample_is_ignored_when_the_newest_is_readable():
    """KN. Unreadability only matters if it is the LATEST word on that slot."""
    s = [
        AllowanceSample("s", ONCHAIN, None, 10.0),
        AllowanceSample("s", ONCHAIN, 11_290_000, 20.0),
    ]
    assert evaluate_funding(s).status is FundingStatus.ABOVE_FLOOR


def test_empty_input_is_undetermined():
    assert evaluate_funding([]).status is FundingStatus.UNDETERMINED


def test_required_deposits_scales_the_floor():
    s = [AllowanceSample("s", ONCHAIN, 6_290_000, 1.0)]
    assert (
        evaluate_funding(s, FundingPolicy(required_deposits=2)).status is FundingStatus.BELOW_FLOOR
    )
    assert (
        evaluate_funding(s, FundingPolicy(required_deposits=1)).status is FundingStatus.ABOVE_FLOOR
    )


def test_amount_zero_is_below_floor_not_undetermined():
    """A measured 0 is a real reading. Only None is 'could not read'."""
    d = evaluate_funding([AllowanceSample("s", ONCHAIN, 0, 1.0)])
    assert d.status is FundingStatus.BELOW_FLOOR


def test_negative_amount_is_rejected_at_construction():
    with pytest.raises(ValueError):
        AllowanceSample("s", ONCHAIN, -1, 1.0)
