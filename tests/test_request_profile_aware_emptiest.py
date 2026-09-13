"""Request-aware emptiest selection: fit first, then requested dimensions only."""

from __future__ import annotations

import importlib
import inspect
import sys
from decimal import Decimal
from pathlib import Path

import pytest

from akash_lease_core import (
    Auction,
    AuctionPolicy,
    AuctionStatus,
    BidObservation,
    BidRejectionReason,
    CapacityFit,
    NodeCapacity,
    PreferredSelection,
    ProviderCapacity,
    ResourceProfile,
    SelectionReason,
)

PREFERRED = frozenset({"lisbon", "sofia", "helsinki", "small", "large", "partial", "complete"})


def _adapter_observation(
    provider: str,
    price: str,
    capacity: ProviderCapacity,
    profile: ResourceProfile,
    index: int,
    *,
    gseq: int = 1,
) -> BidObservation:
    """The consumer boundary: the exact group profile must reach its bid."""
    return BidObservation(
        bid_key=f"bid-{index}",
        provider=provider,
        price=Decimal(price),
        denom="uakt",
        observed_at=float(index),
        capacity=capacity,
        resource_profile=profile,
        gseq=gseq,
    )


def _evaluate(rows, profile: ResourceProfile):
    auction = Auction(
        AuctionPolicy(
            collection_window_seconds=10,
            preferred_providers=PREFERRED,
            preferred_selection=PreferredSelection.EMPTIEST,
        ),
        started_at=0,
    )
    for index, (provider, price, capacity) in enumerate(rows, 1):
        auction.observe(_adapter_observation(provider, price, capacity, profile, index))
    return auction.evaluate(now=11)


def _capacity(
    *,
    cpu: tuple[float, float],
    memory: tuple[float, float] = (900, 1000),
    storage: tuple[float, float] = (900, 1000),
    gpu: tuple[float, float] = (0, 0),
) -> ProviderCapacity:
    node = NodeCapacity(
        cpu_millicores_available=cpu[0],
        memory_bytes_available=memory[0],
        storage_bytes_available=storage[0],
        gpu_count_available=gpu[0],
    )
    return ProviderCapacity.from_totals(
        cpu=cpu,
        memory=memory,
        storage=storage,
        gpu=gpu,
        node_capacities=(node,),
    )


def test_cpu_only_ignores_lisbons_unrelated_gpu_pressure() -> None:
    rows = [
        ("lisbon", "9", _capacity(cpu=(661, 1000), gpu=(2, 15))),
        ("helsinki", "1", _capacity(cpu=(402, 1000))),
        ("sofia", "5", _capacity(cpu=(60, 1000))),
    ]

    result = _evaluate(rows, ResourceProfile(cpu_millicores=50))

    assert result.selected.provider == "lisbon"
    assert result.selection_reason is SelectionReason.EMPTIEST_PREFERRED


@pytest.mark.parametrize(
    "profile",
    [
        pytest.param({}, id="empty"),
        pytest.param({"cpu_millicores": -1}, id="negative"),
        pytest.param({"memory_bytes": 1.5}, id="fractional"),
        pytest.param({"gpu_count": True}, id="boolean"),
    ],
)
def test_resource_profile_refuses_invalid_quantities(profile: dict) -> None:
    with pytest.raises(ValueError):
        ResourceProfile(**profile)


def test_adding_a_gpu_request_changes_which_dimensions_rank() -> None:
    rows = [
        ("lisbon", "9", _capacity(cpu=(800, 1000), gpu=(1, 10))),
        ("helsinki", "1", _capacity(cpu=(700, 1000), gpu=(9, 10))),
    ]

    cpu_only = _evaluate(rows, ResourceProfile(cpu_millicores=100))
    cpu_and_gpu = _evaluate(rows, ResourceProfile(cpu_millicores=100, gpu_count=1))

    assert cpu_only.selected.provider == "lisbon"
    assert cpu_and_gpu.selected.provider == "helsinki"


def test_high_fraction_provider_that_cannot_fit_absolute_request_is_rejected() -> None:
    rows = [
        ("small", "1", _capacity(cpu=(90, 100))),
        ("large", "9", _capacity(cpu=(600, 1000))),
    ]

    result = _evaluate(rows, ResourceProfile(cpu_millicores=500))

    assert result.selected.provider == "large"
    assert [(item.provider, item.reason) for item in result.rejected] == [
        ("small", BidRejectionReason.INSUFFICIENT_CAPACITY)
    ]


def test_unreadable_required_dimension_and_insufficient_fit_are_distinct() -> None:
    profile = ResourceProfile(cpu_millicores=500, memory_bytes=100)
    auction = Auction(
        AuctionPolicy(
            collection_window_seconds=10,
            fallback_window_seconds=0,
            preferred_providers=PREFERRED,
            preferred_selection=PreferredSelection.EMPTIEST,
        ),
        started_at=0,
    )
    auction.observe(
        _adapter_observation(
            "lisbon",
            "1",
            ProviderCapacity.from_totals(
                cpu=(900, 1000),
                node_capacities=(NodeCapacity(cpu_millicores_available=900),),
            ),
            profile,
            1,
        )
    )
    auction.observe(
        _adapter_observation(
            "helsinki",
            "2",
            _capacity(cpu=(400, 1000), memory=(900, 1000)),
            profile,
            2,
        )
    )

    result = auction.evaluate(now=10)

    assert result.status is AuctionStatus.EXPIRED
    assert {item.reason for item in result.rejected} == {
        BidRejectionReason.REQUIRED_CAPACITY_UNREADABLE,
        BidRejectionReason.INSUFFICIENT_CAPACITY,
    }


def test_partial_inventory_can_prove_fit_but_cannot_prove_emptiest() -> None:
    profile = ResourceProfile(cpu_millicores=1)
    partial = ProviderCapacity.from_totals(
        cpu=(10, 10),
        node_capacities=(
            NodeCapacity(cpu_millicores_available=10),
            NodeCapacity(),
        ),
    )
    complete = ProviderCapacity.from_totals(
        cpu=(90, 100),
        node_capacities=(NodeCapacity(cpu_millicores_available=90),),
    )

    assert partial.fit(profile) is CapacityFit.FIT
    assert partial.available_fraction_for(profile) is None
    result = _evaluate([("partial", "9", partial), ("complete", "1", complete)], profile)

    assert result.selected.provider == "complete"
    assert (
        result.selection_reason
        is SelectionReason.EMPTIEST_CAPACITY_INCOMPLETE_FELL_BACK_TO_CHEAPEST
    )
    assert [item.provider for item in result.considered] == ["complete", "partial"]


def test_ranking_completeness_call_site_changes_the_auction_effect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile = ResourceProfile(cpu_millicores=1)
    partial = ProviderCapacity.from_totals(
        cpu=(10, 10),
        node_capacities=(
            NodeCapacity(cpu_millicores_available=10),
            NodeCapacity(),
        ),
    )
    complete = ProviderCapacity.from_totals(
        cpu=(90, 100),
        node_capacities=(NodeCapacity(cpu_millicores_available=90),),
    )
    rows = [("partial", "9", partial), ("complete", "1", complete)]
    normal = _evaluate(rows, profile)

    monkeypatch.setattr(ProviderCapacity, "ranking_complete_for", lambda self, request: True)
    mutated = _evaluate(rows, profile)

    assert normal.selected.provider == "complete"
    assert mutated.selected.provider == "partial"
    assert normal.selection_reason is not mutated.selection_reason
    assert mutated.selection_reason is SelectionReason.EMPTIEST_PREFERRED


def test_missing_profile_is_an_explicit_degraded_decision() -> None:
    rows = [
        ("lisbon", "9", _capacity(cpu=(900, 1000))),
        ("sofia", "1", _capacity(cpu=(100, 1000))),
    ]
    auction = Auction(
        AuctionPolicy(
            collection_window_seconds=10,
            preferred_providers=PREFERRED,
            preferred_selection=PreferredSelection.EMPTIEST,
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
                gseq=1,
            )
        )

    result = auction.evaluate(now=11)

    assert result.selected.provider == "sofia"
    assert (
        result.selection_reason
        is SelectionReason.EMPTIEST_REQUEST_PROFILE_UNAVAILABLE_FELL_BACK_TO_CHEAPEST
    )


def test_profile_without_a_group_cannot_authorize_fit_or_ranking() -> None:
    auction = Auction(
        AuctionPolicy(
            collection_window_seconds=10,
            fallback_window_seconds=0,
            preferred_providers=PREFERRED,
            preferred_selection=PreferredSelection.EMPTIEST,
        ),
        started_at=0,
    )
    auction.observe(
        BidObservation(
            bid_key="unbound",
            provider="lisbon",
            price=Decimal("1"),
            denom="uakt",
            observed_at=0,
            capacity=_capacity(cpu=(900, 1000)),
            resource_profile=ResourceProfile(cpu_millicores=10),
            gseq=None,
        )
    )

    stored = auction.snapshot()["bids"][0]
    assert stored["gseq"] is None
    result = auction.evaluate(now=10)

    assert result.status is AuctionStatus.EXPIRED
    assert result.selected is None
    assert result.rejected[0].reason is BidRejectionReason.RESOURCE_PROFILE_GROUP_UNBOUND


def test_cheapest_cannot_select_a_profile_whose_group_is_unbound() -> None:
    auction = Auction(
        AuctionPolicy(
            collection_window_seconds=0,
            fallback_window_seconds=0,
            preferred_providers=frozenset({"lisbon", "sofia"}),
            preferred_selection=PreferredSelection.CHEAPEST,
        ),
        started_at=0,
    )
    capacity = _capacity(cpu=(900, 1000))
    auction.observe(
        BidObservation(
            bid_key="invalid-cheaper",
            provider="lisbon",
            price=Decimal("1"),
            denom="uakt",
            observed_at=0,
            capacity=capacity,
            resource_profile=ResourceProfile(cpu_millicores=10),
            gseq=None,
        )
    )
    # Legacy consumers supplied neither profile nor group. That shape remains
    # eligible; missing evidence is not falsely presented as group-bound evidence.
    auction.observe(
        BidObservation(
            bid_key="legacy-valid",
            provider="sofia",
            price=Decimal("9"),
            denom="uakt",
            observed_at=0,
            capacity=capacity,
            resource_profile=None,
            gseq=None,
        )
    )

    result = auction.evaluate(now=0)

    assert result.status is AuctionStatus.DECIDED
    assert result.selected is not None
    assert result.selected.bid_key == "legacy-valid"
    assert result.selection_reason is SelectionReason.CHEAPEST_PREFERRED
    assert [(item.bid_key, item.reason) for item in result.rejected] == [
        ("invalid-cheaper", BidRejectionReason.RESOURCE_PROFILE_GROUP_UNBOUND)
    ]


def test_same_group_conflicting_profiles_are_rejected() -> None:
    auction = Auction(AuctionPolicy(), started_at=0)
    capacity = _capacity(cpu=(900, 1000), memory=(900, 1000))
    auction.observe(
        _adapter_observation("lisbon", "1", capacity, ResourceProfile(cpu_millicores=100), 1)
    )

    with pytest.raises(ValueError, match="gseq 1 changed resource profile"):
        auction.observe(
            _adapter_observation(
                "sofia",
                "2",
                capacity,
                ResourceProfile(cpu_millicores=100, memory_bytes=100),
                2,
            )
        )


def test_later_missing_profile_cannot_clear_known_group_profile() -> None:
    auction = Auction(
        AuctionPolicy(
            collection_window_seconds=10,
            fallback_window_seconds=0,
            preferred_providers=frozenset({"lisbon"}),
            preferred_selection=PreferredSelection.EMPTIEST,
        ),
        started_at=0,
    )
    profile = ResourceProfile(cpu_millicores=100)
    capacity = _capacity(cpu=(900, 1000))
    first = _adapter_observation("lisbon", "1", capacity, profile, 1)
    auction.observe(first)
    auction.observe(
        BidObservation(
            bid_key=first.bid_key,
            provider=first.provider,
            price=Decimal("2"),
            denom=first.denom,
            observed_at=2,
            capacity=capacity,
            resource_profile=None,
            gseq=None,
        )
    )

    stored = auction.snapshot()["bids"][0]
    assert stored["resource_profile"]["cpu_millicores"] == 100
    assert stored["gseq"] == 1
    result = auction.evaluate(now=10)
    assert result.status is AuctionStatus.DECIDED
    assert result.selected is not None
    assert result.selected.gseq == 1
    assert result.selection_reason is SelectionReason.EMPTIEST_PREFERRED


def test_same_bid_key_cannot_change_its_established_group() -> None:
    auction = Auction(AuctionPolicy(), started_at=0)
    capacity = _capacity(cpu=(900, 1000))
    first = _adapter_observation("lisbon", "1", capacity, ResourceProfile(cpu_millicores=100), 1)
    auction.observe(first)

    with pytest.raises(ValueError, match="changed gseq from 1 to 2"):
        auction.observe(
            BidObservation(
                bid_key=first.bid_key,
                provider=first.provider,
                price=first.price,
                denom=first.denom,
                observed_at=2,
                capacity=capacity,
                resource_profile=first.resource_profile,
                gseq=2,
            )
        )

    stored = auction.snapshot()["bids"][0]
    assert stored["gseq"] == 1


def test_late_profile_backfills_earlier_bid_for_same_group_order_independently() -> None:
    auction = Auction(AuctionPolicy(), started_at=0)
    capacity = _capacity(cpu=(900, 1000))
    auction.observe(
        BidObservation(
            bid_key="first",
            provider="lisbon",
            price=Decimal("1"),
            denom="uakt",
            observed_at=1,
            capacity=capacity,
            resource_profile=None,
            gseq=1,
        )
    )
    profile = ResourceProfile(cpu_millicores=100)
    auction.observe(_adapter_observation("sofia", "2", capacity, profile, 2))

    profiles = [item["resource_profile"] for item in auction.snapshot()["bids"]]
    encoded_profile = {
        "cpu_millicores": 100,
        "memory_bytes": 0,
        "storage_bytes": 0,
        "gpu_count": 0,
        "replicas": [
            {"cpu_millicores": 100, "memory_bytes": 0, "storage_bytes": 0, "gpu_count": 0}
        ],
    }
    assert profiles == [
        encoded_profile,
        encoded_profile,
    ]


def test_different_groups_may_carry_different_profiles() -> None:
    auction = Auction(AuctionPolicy(), started_at=0)
    capacity = _capacity(cpu=(900, 1000), memory=(900, 1000))
    auction.observe(
        _adapter_observation("lisbon", "1", capacity, ResourceProfile(cpu_millicores=100), 1)
    )
    second = _adapter_observation(
        "sofia",
        "2",
        capacity,
        ResourceProfile(memory_bytes=100),
        2,
        gseq=2,
    )
    auction.observe(second)

    assert len(auction.snapshot()["bids"]) == 2


def test_call_site_profile_propagation_effect_mutation_changes_the_verdict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Deleting the adapter's profile wiring must break the end-to-end property."""
    source = inspect.getsource(_adapter_observation)
    target = "        resource_profile=profile,\n"
    replacement = "        resource_profile=None,\n"
    assert source.count(target) == 1
    mutated_source = source.replace(target, replacement)
    assert mutated_source.count(replacement) == 1

    module_name = "mutated_profile_adapter"
    module_path = tmp_path / f"{module_name}.py"
    module_path.write_text(
        "from decimal import Decimal\n"
        "from akash_lease_core import BidObservation, ProviderCapacity, ResourceProfile\n\n"
        + mutated_source
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.delitem(sys.modules, module_name, raising=False)
    mutated_adapter = importlib.import_module(module_name)._adapter_observation
    rows = [
        ("lisbon", "9", _capacity(cpu=(900, 1000))),
        ("sofia", "1", _capacity(cpu=(100, 1000))),
    ]
    profile = ResourceProfile(cpu_millicores=10)
    normal = _evaluate(rows, profile)

    auction = Auction(
        AuctionPolicy(
            collection_window_seconds=10,
            preferred_providers=PREFERRED,
            preferred_selection=PreferredSelection.EMPTIEST,
        ),
        started_at=0,
    )
    for index, (provider, price, capacity) in enumerate(rows, 1):
        observation = mutated_adapter(provider, price, capacity, profile, index)
        assert observation.resource_profile is None
        auction.observe(observation)
    mutated = auction.evaluate(now=11)

    assert normal.selected.provider == "lisbon"
    assert mutated.selected.provider == "sofia"
    assert normal.considered != mutated.considered
    assert (
        mutated.selection_reason
        is SelectionReason.EMPTIEST_REQUEST_PROFILE_UNAVAILABLE_FELL_BACK_TO_CHEAPEST
    )
