"""Emptiest-first preferred selection, and the anti-affinity that makes it work.

Motivating measurement (operator, 2026-08-25): one datacenter is far larger than
its siblings and sits below 10% utilisation while the others run near 50%.
Cheapest-first crowds the busy providers, so a three-region placement fails on
the ones with no room.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from akash_lease_core.auction import (
    Auction,
    AuctionPolicy,
    BidObservation,
    PreferredSelection,
)
from akash_lease_core.capacity import ProviderCapacity

PREFERRED = frozenset({"lisbon", "sofia", "hel"})


def _auction(mode: PreferredSelection, fleet):
    policy = AuctionPolicy(
        collection_window_seconds=10,
        preferred_providers=PREFERRED,
        preferred_selection=mode,
    )
    auction = Auction(policy, started_at=0.0)
    for index, (provider, price, capacity) in enumerate(fleet):
        auction.observe(
            BidObservation(
                bid_key=f"bid-{index}",
                provider=provider,
                price=Decimal(price),
                denom="uakt",
                observed_at=1.0 + index,
                capacity=capacity,
            )
        )
    return auction


# The measured shape: the big empty one is also the dearest.
FLEET = [
    ("lisbon", "9", ProviderCapacity(cpu=0.92, memory=0.90)),
    ("sofia", "1", ProviderCapacity(cpu=0.50, memory=0.50)),
    ("hel", "5", ProviderCapacity(cpu=0.40, memory=0.45)),
]


def test_cheapest_remains_the_default_and_is_unchanged() -> None:
    result = _auction(PreferredSelection.CHEAPEST, FLEET).evaluate(now=11.0)
    assert result.selected.provider == "sofia"
    assert result.selection_reason == "cheapest_preferred"


def test_emptiest_prefers_headroom_over_price() -> None:
    result = _auction(PreferredSelection.EMPTIEST, FLEET).evaluate(now=11.0)
    assert result.selected.provider == "lisbon"
    assert result.selection_reason == "emptiest_preferred"


def test_the_binding_dimension_decides_not_the_roomiest_one() -> None:
    """⭐ A provider 92% free on CPU and 5% free on memory cannot take a
    memory-bound workload. Ranking on the maximum -- or an average -- would
    recommend exactly the provider about to refuse the bid."""
    fleet = [
        ("lisbon", "9", ProviderCapacity(cpu=0.92, memory=0.05)),
        ("sofia", "1", ProviderCapacity(cpu=0.50, memory=0.50)),
    ]
    result = _auction(PreferredSelection.EMPTIEST, fleet).evaluate(now=11.0)
    assert result.selected.provider == "sofia"


def test_a_degraded_selection_does_not_report_as_the_mode_requested() -> None:
    """⛔ Silently returning ``cheapest_preferred`` would make an UNMEASURABLE
    fleet indistinguishable from a measured one that happened to agree."""
    fleet = [(p, price, None) for p, price, _ in FLEET]
    result = _auction(PreferredSelection.EMPTIEST, fleet).evaluate(now=11.0)
    assert result.selected.provider == "sofia"
    assert result.selection_reason == "emptiest_unavailable_fell_back_to_cheapest"


def test_three_placements_on_ONE_snapshot_land_on_three_providers() -> None:
    """⭐⭐ THE OPERATOR'S ACTUAL GOAL: room in all three at once.

    A multi-region deployment evaluates several auctions against one capacity
    reading. Without anti-affinity every auction sees the same emptiest provider
    and picks it -- a thundering herd that is strictly worse than cheapest-first.
    """
    taken: list[str] = []
    for _ in range(3):
        result = _auction(PreferredSelection.EMPTIEST, FLEET).evaluate(
            now=11.0, already_selected=frozenset(taken)
        )
        taken.append(result.selected.provider)
    assert taken == ["lisbon", "sofia", "hel"]
    assert len(set(taken)) == 3


def test_KNOWN_NEGATIVE_without_anti_affinity_all_three_pile_onto_one() -> None:
    """The control that proves the previous test is not vacuous: drop the
    already-selected set and the same fleet returns the same provider 3x."""
    picks = [
        _auction(PreferredSelection.EMPTIEST, FLEET).evaluate(now=11.0).selected.provider
        for _ in range(3)
    ]
    assert picks == ["lisbon", "lisbon", "lisbon"]
    assert len(set(picks)) == 1


def test_anti_affinity_deprioritises_it_does_not_exclude() -> None:
    """⚠ If the already-taken provider is the ONLY preferred bidder, taking it
    beats failing to place. This changes the ORDER, never the eligibility."""
    fleet = [("lisbon", "9", ProviderCapacity(cpu=0.92))]
    result = _auction(PreferredSelection.EMPTIEST, fleet).evaluate(
        now=11.0, already_selected=frozenset({"lisbon"})
    )
    assert result.selected.provider == "lisbon"


class TestCapacityArithmetic:
    def test_total_zero_is_not_applicable_not_zero_percent_free(self) -> None:
        """A provider offering no GPUs is not 0% free on GPU. Scoring it zero
        would rank every CPU-only provider as completely full."""
        capacity = ProviderCapacity.from_totals(gpu=(0, 0), cpu=(8, 10))
        assert capacity.gpu is None
        assert capacity.available_fraction() == pytest.approx(0.8)

    def test_unreadable_is_neither_full_nor_empty(self) -> None:
        assert ProviderCapacity().available_fraction() is None
        assert ProviderCapacity().is_readable is False

    @pytest.mark.parametrize("bad", [float("nan"), float("inf"), -0.1, 1.1])
    def test_a_value_that_could_silently_win_min_is_rejected(self, bad: float) -> None:
        with pytest.raises(ValueError):
            ProviderCapacity(cpu=bad)
