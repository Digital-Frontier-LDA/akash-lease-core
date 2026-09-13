"""Per-node placement proof alongside aggregate group capacity."""

from __future__ import annotations

import importlib.util
import json
import sys
from decimal import Decimal
from pathlib import Path

import pytest

import akash_lease_core.capacity as capacity_module
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
    ReplicaProfile,
    ResourceProfile,
    from_provider_status,
)

FIXTURES = Path(__file__).parent / "fixtures"


def _capacity(fixture: str) -> ProviderCapacity:
    return from_provider_status(json.loads((FIXTURES / fixture).read_text()))


def _two_eight_cpu_replicas() -> ResourceProfile:
    replica = ReplicaProfile(cpu_millicores=8_000)
    return ResourceProfile(cpu_millicores=16_000, replicas=(replica, replica))


def test_sofia_like_aggregate_29_9_cpu_cannot_fit_an_8_cpu_replica() -> None:
    capacity = _capacity("provider_status_sofia_fragmented.json")

    assert capacity.cpu_millicores_available == 29_900
    assert max(node.cpu_millicores_available for node in capacity.node_capacities) == 5_700
    assert capacity.fit(_two_eight_cpu_replicas()) is CapacityFit.INSUFFICIENT_CAPACITY


def test_same_aggregate_fits_when_one_node_has_8_cpu_free() -> None:
    capacity = _capacity("provider_status_sofia_placeable.json")

    assert capacity.cpu_millicores_available == 29_900
    assert max(node.cpu_millicores_available for node in capacity.node_capacities) == 8_000
    assert capacity.fit(_two_eight_cpu_replicas()) is CapacityFit.FIT


def test_one_fitting_node_does_not_replace_the_full_group_aggregate_check() -> None:
    capacity = _capacity("provider_status_sofia_placeable.json")
    replica = ReplicaProfile(cpu_millicores=8_000)
    profile = ResourceProfile(
        cpu_millicores=32_000,
        replicas=(replica, replica, replica, replica),
    )

    assert capacity.fit(profile) is CapacityFit.INSUFFICIENT_CAPACITY


def test_missing_node_breakdown_is_typed_unreadable() -> None:
    aggregate_only = ProviderCapacity.from_totals(cpu=(29_900, 432_000))

    assert (
        aggregate_only.fit(_two_eight_cpu_replicas()) is CapacityFit.REQUIRED_DIMENSION_UNREADABLE
    )


def test_replica_dimensions_must_fit_together_on_the_same_node() -> None:
    capacity = ProviderCapacity.from_totals(
        cpu=(10_000, 20_000),
        memory=(10_000, 20_000),
        node_capacities=(
            NodeCapacity(cpu_millicores_available=8_000, memory_bytes_available=2_000),
            NodeCapacity(cpu_millicores_available=2_000, memory_bytes_available=8_000),
        ),
    )
    profile = ResourceProfile(
        cpu_millicores=8_000,
        memory_bytes=8_000,
        replicas=(ReplicaProfile(cpu_millicores=8_000, memory_bytes=8_000),),
    )

    assert capacity.fit(profile) is CapacityFit.INSUFFICIENT_CAPACITY


def test_auction_preserves_the_typed_insufficient_rejection() -> None:
    profile = _two_eight_cpu_replicas()
    auction = Auction(
        AuctionPolicy(
            collection_window_seconds=10,
            fallback_window_seconds=0,
            preferred_providers=frozenset({"sofia"}),
            preferred_selection=PreferredSelection.EMPTIEST,
        ),
        started_at=0,
    )
    auction.observe(
        BidObservation(
            bid_key="sofia/1/1/1",
            provider="sofia",
            price=Decimal("1"),
            denom="uakt",
            observed_at=1,
            capacity=_capacity("provider_status_sofia_fragmented.json"),
            resource_profile=profile,
            gseq=1,
        )
    )

    result = auction.evaluate(now=10)

    assert result.status is AuctionStatus.EXPIRED
    assert [(item.provider, item.reason) for item in result.rejected] == [
        ("sofia", BidRejectionReason.INSUFFICIENT_CAPACITY)
    ]


def test_replica_shapes_must_sum_to_the_exact_group_aggregate() -> None:
    with pytest.raises(ValueError, match="replicas sum to 8000 cpu_millicores"):
        ResourceProfile(
            cpu_millicores=16_000,
            replicas=(ReplicaProfile(cpu_millicores=8_000),),
        )


def test_sum_only_effect_mutation_turns_the_sofia_verdict_green(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Deleting the exact node-fit block recreates #48 and changes the verdict."""
    source = Path(capacity_module.__file__).read_text()
    target = """        for replica in profile.replicas:
            node_fits = tuple(node.fit(replica) for node in self.node_capacities)
            if CapacityFit.FIT in node_fits:
                continue
            if CapacityFit.REQUIRED_DIMENSION_UNREADABLE in node_fits:
                return CapacityFit.REQUIRED_DIMENSION_UNREADABLE
            return CapacityFit.INSUFFICIENT_CAPACITY
"""
    assert source.count(target) == 1
    mutated_source = source.replace(target, "")
    assert target not in mutated_source

    module_name = "mutated_sum_only_capacity"
    module_path = tmp_path / f"{module_name}.py"
    module_path.write_text(mutated_source)
    spec = importlib.util.spec_from_file_location(module_name, module_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, module_name, module)
    spec.loader.exec_module(module)

    status = json.loads((FIXTURES / "provider_status_sofia_fragmented.json").read_text())
    replica = module.ReplicaProfile(cpu_millicores=8_000)
    profile = module.ResourceProfile(cpu_millicores=16_000, replicas=(replica, replica))

    assert module.from_provider_status(status).fit(profile) is module.CapacityFit.FIT
