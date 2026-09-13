"""Request-aware emptiest selection: fit first, then requested dimensions only."""

from __future__ import annotations

import inspect
from decimal import Decimal

import pytest

from akash_lease_core import (
    Auction,
    AuctionPolicy,
    AuctionStatus,
    BidObservation,
    BidRejectionReason,
    PreferredSelection,
    ProviderCapacity,
    ResourceProfile,
    SelectionReason,
)

PREFERRED = frozenset({"lisbon", "sofia", "helsinki", "small", "large"})


def _adapter_observation(
    provider: str,
    price: str,
    capacity: ProviderCapacity,
    profile: ResourceProfile,
    index: int,
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
    return ProviderCapacity.from_totals(
        cpu=cpu,
        memory=memory,
        storage=storage,
        gpu=gpu,
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
            ProviderCapacity.from_totals(cpu=(900, 1000)),
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
            )
        )

    result = auction.evaluate(now=11)

    assert result.selected.provider == "sofia"
    assert (
        result.selection_reason
        is SelectionReason.EMPTIEST_REQUEST_PROFILE_UNAVAILABLE_FELL_BACK_TO_CHEAPEST
    )


def test_call_site_profile_propagation_effect_mutation_changes_the_verdict() -> None:
    """Deleting the adapter's profile wiring must break the end-to-end property."""
    source = inspect.getsource(_adapter_observation)
    target = "        resource_profile=profile,\n"
    replacement = "        resource_profile=None,\n"
    assert source.count(target) == 1
    mutated_source = source.replace(target, replacement)
    assert mutated_source.count(replacement) == 1

    namespace = {
        "BidObservation": BidObservation,
        "Decimal": Decimal,
        "ProviderCapacity": ProviderCapacity,
        "ResourceProfile": ResourceProfile,
    }
    exec(mutated_source, namespace)
    mutated_adapter = namespace["_adapter_observation"]
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
