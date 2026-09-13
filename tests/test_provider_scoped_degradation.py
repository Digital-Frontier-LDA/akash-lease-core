"""#50: degradation is provider-scoped, fallbacks keep anti-affinity, verdicts stay honest.

Measured on v0.15.0 by DEV1-blazing's post-merge review:

* L1 -- Q (60% free) and R (30%) both complete select Q, but ONE partially readable
  bidder P turned the whole auction into a cheapest fallback that selected R.
* L2 -- in that degraded auction ``already_selected={Q}`` still selected Q: the
  cheapest fallbacks never consulted ``taken``.
* L3 -- an aggregate that contradicts its own node list was reported as
  ``insufficient_capacity`` in both directions.
* L4 -- an aggregate below the request with no node list proves insufficiency, but
  was reported as unreadable.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from akash_lease_core import (
    Auction,
    AuctionPolicy,
    BidObservation,
    BidRejectionReason,
    CapacityFit,
    NodeCapacity,
    PreferredSelection,
    ProviderCapacity,
    ResourceProfile,
    SelectionReason,
)

PROFILE = ResourceProfile(cpu_millicores=100)


def _complete(free: int, total: int = 1000) -> ProviderCapacity:
    return ProviderCapacity.from_totals(
        cpu=(free, total), node_capacities=(NodeCapacity(cpu_millicores_available=free),)
    )


def _partial(free: int, total: int = 1000) -> ProviderCapacity:
    """Fit is provable from the readable node; the free fraction is not."""
    return ProviderCapacity.from_totals(
        cpu=(free, total),
        node_capacities=(NodeCapacity(cpu_millicores_available=free), NodeCapacity()),
    )


def _evaluate(rows, *, already_selected=None, profile=PROFILE, policy=PreferredSelection.EMPTIEST):
    auction = Auction(
        AuctionPolicy(
            collection_window_seconds=10,
            preferred_providers=frozenset(provider for provider, _, _ in rows),
            preferred_selection=policy,
        ),
        started_at=0,
    )
    for index, (provider, price, capacity) in enumerate(rows, 1):
        auction.observe(
            BidObservation(
                bid_key=f"bid-{index}",
                provider=provider,
                price=Decimal(price),
                denom="uakt",
                observed_at=float(index),
                capacity=capacity,
                resource_profile=profile,
                gseq=1,
            )
        )
    return auction.evaluate(now=11, already_selected=already_selected)


# ── L1 ──────────────────────────────────────────────────────────────────────


def test_complete_pool_selects_the_emptiest() -> None:
    result = _evaluate([("q", "9", _complete(600)), ("r", "1", _complete(300))])

    assert result.selected.provider == "q"
    assert result.selection_reason is SelectionReason.EMPTIEST_PREFERRED


def test_one_partial_bidder_does_not_remove_emptiest_from_the_auction() -> None:
    result = _evaluate(
        [("q", "9", _complete(600)), ("r", "1", _complete(300)), ("p", "5", _partial(900))]
    )

    assert result.selected.provider == "q"
    assert result.selection_reason is SelectionReason.EMPTIEST_PREFERRED
    # The partial bidder is still eligible; it ranks after every complete provider.
    assert [item.provider for item in result.considered] == ["q", "r", "p"]


def test_an_all_partial_pool_is_an_explicit_cheapest_fallback() -> None:
    result = _evaluate([("q", "9", _partial(600)), ("r", "1", _partial(300))])

    assert result.selected.provider == "r"
    assert (
        result.selection_reason
        is SelectionReason.EMPTIEST_CAPACITY_INCOMPLETE_FELL_BACK_TO_CHEAPEST
    )


# ── L2 ──────────────────────────────────────────────────────────────────────


def test_degraded_pool_still_steps_away_from_an_already_selected_provider() -> None:
    rows = [("q", "1", _complete(600)), ("r", "5", _complete(300)), ("p", "3", _partial(900))]

    result = _evaluate(rows, already_selected=frozenset({"q"}))

    assert result.selected.provider == "r"
    assert result.selection_reason is SelectionReason.EMPTIEST_PREFERRED


def test_capacity_incomplete_fallback_honours_already_selected() -> None:
    rows = [("q", "1", _partial(600)), ("r", "5", _partial(300))]

    result = _evaluate(rows, already_selected=frozenset({"q"}))

    assert result.selected.provider == "r"
    assert (
        result.selection_reason
        is SelectionReason.EMPTIEST_CAPACITY_INCOMPLETE_FELL_BACK_TO_CHEAPEST
    )
    assert [item.provider for item in result.considered] == ["r", "q"]


def test_an_unscorable_winner_reports_the_degraded_reason() -> None:
    """Every complete provider is taken, so the pick was made by price, not emptiness."""
    rows = [("q", "1", _complete(600)), ("p", "5", _partial(900))]

    result = _evaluate(rows, already_selected=frozenset({"q"}))

    assert result.selected.provider == "p"
    assert (
        result.selection_reason
        is SelectionReason.EMPTIEST_CAPACITY_INCOMPLETE_FELL_BACK_TO_CHEAPEST
    )


def test_profile_unavailable_fallback_honours_already_selected() -> None:
    rows = [("q", "1", _complete(600)), ("r", "5", _complete(300))]

    result = _evaluate(rows, already_selected=frozenset({"q"}), profile=None)

    assert result.selected.provider == "r"
    assert (
        result.selection_reason
        is SelectionReason.EMPTIEST_REQUEST_PROFILE_UNAVAILABLE_FELL_BACK_TO_CHEAPEST
    )


@pytest.mark.parametrize("profile", [PROFILE, None])
def test_fallback_anti_affinity_deprioritises_but_never_excludes(profile) -> None:
    capacity = _partial(600) if profile is not None else _complete(600)

    result = _evaluate([("q", "1", capacity)], already_selected=frozenset({"q"}), profile=profile)

    assert result.selected.provider == "q"


def test_cheapest_policy_is_untouched_by_already_selected() -> None:
    rows = [("q", "1", _complete(600)), ("r", "5", _complete(300))]

    result = _evaluate(rows, already_selected=frozenset({"q"}), policy=PreferredSelection.CHEAPEST)

    assert result.selected.provider == "q"
    assert result.selection_reason is SelectionReason.CHEAPEST_PREFERRED


# ── L3 ──────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("aggregate", "nodes"),
    [
        pytest.param(5_000, (16_000,), id="aggregate-below-its-nodes"),
        pytest.param(20_000, (4_000,), id="aggregate-above-a-complete-node-list"),
        pytest.param(20_000, (16_000,), id="contradiction-that-would-otherwise-fit"),
    ],
)
def test_an_aggregate_contradicting_its_nodes_is_unreadable(aggregate, nodes) -> None:
    capacity = ProviderCapacity.from_totals(
        cpu=(aggregate, 32_000),
        node_capacities=tuple(NodeCapacity(cpu_millicores_available=n) for n in nodes),
    )
    profile = ResourceProfile(cpu_millicores=8_000)

    assert capacity.fit(profile) is CapacityFit.REQUIRED_DIMENSION_UNREADABLE
    result = _evaluate([("c", "1", capacity)], profile=profile)
    assert [item.reason for item in result.rejected] == [
        BidRejectionReason.REQUIRED_CAPACITY_UNREADABLE
    ]


def test_a_readable_node_above_its_aggregate_is_a_contradiction_even_when_partial() -> None:
    """An aggregate may omit unreadable nodes, but never hold less than a readable one."""
    capacity = ProviderCapacity.from_totals(
        cpu=(5_000, 32_000),
        node_capacities=(NodeCapacity(cpu_millicores_available=16_000), NodeCapacity()),
    )

    assert (
        capacity.fit(ResourceProfile(cpu_millicores=4_000))
        is CapacityFit.REQUIRED_DIMENSION_UNREADABLE
    )


def test_an_aggregate_omitting_an_unreadable_node_is_not_a_contradiction() -> None:
    """The adapter sums only readable nodes, so the aggregate is a lower bound there."""
    capacity = ProviderCapacity.from_totals(
        cpu=(16_000, 32_000),
        node_capacities=(NodeCapacity(cpu_millicores_available=16_000), NodeCapacity()),
    )

    assert capacity.fit(ResourceProfile(cpu_millicores=8_000)) is CapacityFit.FIT


def test_consistent_insufficiency_is_still_proven() -> None:
    capacity = ProviderCapacity.from_totals(
        cpu=(4_000, 32_000), node_capacities=(NodeCapacity(cpu_millicores_available=4_000),)
    )

    assert capacity.fit(ResourceProfile(cpu_millicores=8_000)) is CapacityFit.INSUFFICIENT_CAPACITY


# ── L4 ──────────────────────────────────────────────────────────────────────


def test_aggregate_below_request_without_nodes_is_proven_insufficient() -> None:
    capacity = ProviderCapacity.from_totals(cpu=(100, 1000))

    assert capacity.fit(ResourceProfile(cpu_millicores=500)) is CapacityFit.INSUFFICIENT_CAPACITY


def test_sufficient_aggregate_without_nodes_stays_unreadable() -> None:
    capacity = ProviderCapacity.from_totals(cpu=(900, 1000))

    assert (
        capacity.fit(ResourceProfile(cpu_millicores=500))
        is CapacityFit.REQUIRED_DIMENSION_UNREADABLE
    )
