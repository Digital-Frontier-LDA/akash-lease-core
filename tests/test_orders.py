"""Leaked-order sweep.

The three controls TEAMLEAD required are first and named as such: a 3h-old open
order MUST be flagged, an order with an active lease MUST NOT be, and a 5-minute
-old order MUST NOT be. A suite where every case comes out the same way measures
nothing, so each limb below is also driven in both directions.
"""

from __future__ import annotations

import pytest

from akash_lease_core import (
    LeaseEvidence,
    OrderObservation,
    OrderPolicy,
    OrderStatus,
    confirm_close,
    evaluate_order,
    page_is_last,
    parse_escrow_open,
    select_batch,
)

HOUR = 3600.0


def obs(**kw) -> OrderObservation:
    base = dict(
        dseq="1800000000001",
        owner="akash1cklqag",
        deployment_state="active",
        lease_count=0,
        lease_evidence=LeaseEvidence.CHAIN,
        age_seconds=3 * HOUR,
        group_states=("open",),
        name="",
    )
    base.update(kw)
    return OrderObservation(**base)


# --- the three required controls -------------------------------------------------


def test_KNOWN_POSITIVE_open_order_aged_3h_is_closeable():
    d = evaluate_order(obs())
    assert d.status is OrderStatus.CLOSEABLE, d.reason
    assert d.closeable


def test_KNOWN_NEGATIVE_an_order_with_an_active_lease_is_never_closeable():
    d = evaluate_order(obs(lease_count=1, group_states=("active",)))
    assert d.status is OrderStatus.HAS_LEASE, d.reason
    assert not d.closeable


def test_KNOWN_NEGATIVE_an_order_five_minutes_old_is_never_closeable():
    d = evaluate_order(obs(age_seconds=300.0))
    assert d.status is OrderStatus.TOO_YOUNG, d.reason
    assert not d.closeable


# --- the age floor ---------------------------------------------------------------


@pytest.mark.parametrize("age", [0.0, 96.0, 138.0, 450.0, 7199.0])
def test_anything_below_the_floor_is_refused(age):
    """96s and 138s are the real population that was nearly closed mid-auction."""
    assert evaluate_order(obs(age_seconds=age)).status is OrderStatus.TOO_YOUNG


def test_exactly_at_the_floor_is_allowed():
    assert evaluate_order(obs(age_seconds=7200.0)).status is OrderStatus.CLOSEABLE


def test_an_unreadable_age_is_undetermined_not_old_enough():
    assert evaluate_order(obs(age_seconds=None)).status is OrderStatus.UNDETERMINED


# --- lease evidence: the instrument that lies in BOTH directions ------------------


def test_console_list_evidence_cannot_authorise_even_when_it_says_zero():
    d = evaluate_order(obs(lease_evidence=LeaseEvidence.CONSOLE_LIST, lease_count=0))
    assert d.status is OrderStatus.UNDETERMINED, d.reason


def test_console_list_evidence_is_refused_before_anything_else_is_trusted():
    """A list-sourced reading must not benefit from other fields looking fine."""
    d = evaluate_order(obs(lease_evidence=LeaseEvidence.CONSOLE_LIST, age_seconds=10 * HOUR))
    assert d.status is OrderStatus.UNDETERMINED


@pytest.mark.parametrize("ev", [LeaseEvidence.CHAIN, LeaseEvidence.DEPLOYMENT_DETAIL])
def test_chain_and_detail_evidence_may_authorise(ev):
    assert evaluate_order(obs(lease_evidence=ev)).status is OrderStatus.CLOSEABLE


def test_an_unreadable_lease_count_is_undetermined_not_zero():
    assert evaluate_order(obs(lease_count=None)).status is OrderStatus.UNDETERMINED


# --- deployment / group state ----------------------------------------------------


def test_a_closed_deployment_has_nothing_to_close():
    assert evaluate_order(obs(deployment_state="closed")).status is OrderStatus.NOT_ACTIVE


def test_an_unreadable_deployment_state_is_undetermined():
    assert evaluate_order(obs(deployment_state=None)).status is OrderStatus.UNDETERMINED


def test_contradicting_instruments_are_undetermined_not_a_tiebreak():
    """lease_count says 0, a group says active. That is an unknown, not a zero."""
    d = evaluate_order(obs(lease_count=0, group_states=("open", "active")))
    assert d.status is OrderStatus.UNDETERMINED, d.reason


def test_absent_group_states_do_not_block_a_decision():
    assert evaluate_order(obs(group_states=None)).status is OrderStatus.CLOSEABLE


# --- protection and exclusion ----------------------------------------------------


def test_the_protected_dseq_is_never_closeable():
    assert evaluate_order(obs(dseq="1784532174413")).status is OrderStatus.PROTECTED


def test_protection_wins_over_every_other_signal():
    d = evaluate_order(obs(dseq="1784532174413", age_seconds=10 * HOUR, lease_count=0))
    assert d.status is OrderStatus.PROTECTED


@pytest.mark.parametrize("name", ["just-akash-runner", "just-akash-pool-7"])
def test_sibling_repo_objects_are_excluded(name):
    assert evaluate_order(obs(name=name)).status is OrderStatus.EXCLUDED


def test_borduas_owned_objects_are_excluded():
    assert evaluate_order(obs(owner="akash1borduasxyz")).status is OrderStatus.EXCLUDED


def test_exclusion_is_not_overzealous():
    """A guard that excludes everything is indistinguishable from a broken sweep."""
    assert (
        evaluate_order(obs(name="blazing-ci-pool", owner="akash1cklqag")).status
        is OrderStatus.CLOSEABLE
    )


# --- batching --------------------------------------------------------------------


def test_the_batch_is_capped():
    ds = [evaluate_order(obs(dseq=str(1800000000000 + i))) for i in range(50)]
    assert all(d.closeable for d in ds)
    assert len(select_batch(ds)) == 20


def test_the_batch_contains_only_closeable_decisions():
    mixed = [
        evaluate_order(obs(dseq="1")),
        evaluate_order(obs(dseq="2", lease_count=3, group_states=None)),
        evaluate_order(obs(dseq="3", age_seconds=60.0)),
        evaluate_order(obs(dseq="4")),
    ]
    picked = select_batch(mixed)
    assert [d.dseq for d in picked] == ["1", "4"]


def test_a_custom_batch_limit_is_honoured():
    ds = [evaluate_order(obs(dseq=str(i))) for i in range(10)]
    assert len(select_batch(ds, OrderPolicy(batch_limit=3))) == 3


# --- re-verification before the delete -------------------------------------------


def test_confirm_close_refuses_when_the_order_went_active_between_scan_and_close():
    """The real incident: CLOSEABLE at scan, ACTIVE by the time we deleted."""
    assert evaluate_order(obs()).status is OrderStatus.CLOSEABLE
    fresh = obs(lease_count=1, group_states=("active",))
    assert confirm_close(fresh).status is OrderStatus.HAS_LEASE


def test_confirm_close_still_passes_an_order_that_really_is_dead():
    assert confirm_close(obs()).status is OrderStatus.CLOSEABLE


# --- escrow parsing: the nested dict ---------------------------------------------


def test_escrow_state_is_read_from_the_NESTED_key():
    assert parse_escrow_open({"state": {"state": "open"}}) is True
    assert parse_escrow_open({"state": {"state": "closed"}}) is False


def test_a_flat_escrow_state_still_parses():
    assert parse_escrow_open({"state": "open"}) is True
    assert parse_escrow_open({"state": "closed"}) is False


@pytest.mark.parametrize(
    "bad", [None, [], "open", {}, {"state": {}}, {"state": {"state": 5}}, {"state": 7}]
)
def test_an_unrecognised_escrow_shape_is_None_not_False(bad):
    """False would read as 'closed' and mark a live escrow as already reclaimed."""
    assert parse_escrow_open(bad) is None


# --- pagination ------------------------------------------------------------------


def test_a_full_page_is_not_the_last():
    assert page_is_last([1, 2, 3], 3) is False


def test_a_short_page_is_the_last():
    assert page_is_last([1, 2], 3) is True
    assert page_is_last([], 3) is True


def test_pagination_total_echoing_the_limit_cannot_truncate_us():
    """limit=1 makes the API report total=1 regardless of the real count. The
    stop condition must not consult it at all — one full page is never the end."""
    assert page_is_last([{"dseq": "1"}], 1) is False


def test_a_nonsense_limit_is_rejected():
    with pytest.raises(ValueError):
        page_is_last([], 0)


# --- observation validation ------------------------------------------------------


def test_owner_must_be_supplied_since_the_list_cannot_provide_it():
    with pytest.raises(ValueError, match="owner"):
        obs(owner="")


def test_a_negative_lease_count_is_rejected():
    with pytest.raises(ValueError):
        obs(lease_count=-1)


def test_a_bool_is_not_a_lease_count():
    with pytest.raises(ValueError):
        obs(lease_count=True)
