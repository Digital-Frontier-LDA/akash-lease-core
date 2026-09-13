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


def _cpu_capacity(*available: int, unreadable_node: bool = False) -> ProviderCapacity:
    nodes = tuple(NodeCapacity(cpu_millicores_available=value) for value in available)
    if unreadable_node:
        nodes += (NodeCapacity(),)
    total = sum(available)
    return ProviderCapacity.from_totals(cpu=(total, total), node_capacities=nodes)


def _load_mutated_capacity(
    source: str, module_name: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    module_path = tmp_path / f"{module_name}.py"
    module_path.write_text(source)
    spec = importlib.util.spec_from_file_location(module_name, module_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, module_name, module)
    spec.loader.exec_module(module)
    return module


def test_sofia_like_aggregate_29_9_cpu_cannot_fit_an_8_cpu_replica() -> None:
    capacity = _capacity("provider_status_sofia_fragmented.json")

    assert capacity.cpu_millicores_available == 29_900
    assert max(node.cpu_millicores_available for node in capacity.node_capacities) == 5_700
    assert capacity.fit(_two_eight_cpu_replicas()) is CapacityFit.INSUFFICIENT_CAPACITY


def test_same_aggregate_fits_when_two_nodes_each_have_8_cpu_free() -> None:
    capacity = _capacity("provider_status_sofia_placeable.json")

    assert capacity.cpu_millicores_available == 29_900
    assert max(node.cpu_millicores_available for node in capacity.node_capacities) == 8_000
    assert capacity.fit(_two_eight_cpu_replicas()) is CapacityFit.FIT


@pytest.mark.parametrize(
    ("node_frees", "expected"),
    [
        pytest.param((12_000, 2_000, 2_000), CapacityFit.INSUFFICIENT_CAPACITY, id="12-2-2"),
        pytest.param((16_000,), CapacityFit.FIT, id="16"),
        pytest.param((8_000, 8_000), CapacityFit.FIT, id="8-8"),
    ],
)
def test_two_eight_cpu_replicas_consume_node_capacity(
    node_frees: tuple[int, ...], expected: CapacityFit
) -> None:
    assert _cpu_capacity(*node_frees).fit(_two_eight_cpu_replicas()) is expected


@pytest.mark.parametrize(
    ("node_frees", "expected"),
    [
        pytest.param((12_000, 6_000), CapacityFit.FIT, id="12-6"),
        pytest.param((10_000, 10_000), CapacityFit.INSUFFICIENT_CAPACITY, id="10-10"),
    ],
)
def test_three_six_cpu_replicas_are_packed_exactly(
    node_frees: tuple[int, ...], expected: CapacityFit
) -> None:
    replica = ReplicaProfile(cpu_millicores=6_000)
    profile = ResourceProfile(cpu_millicores=18_000, replicas=(replica, replica, replica))

    assert _cpu_capacity(*node_frees).fit(profile) is expected


def test_two_multidimensional_replicas_exceed_one_nodes_memory() -> None:
    capacity = ProviderCapacity.from_totals(
        cpu=(16_000, 16_000),
        memory=(12 * 2**30, 12 * 2**30),
        node_capacities=(
            NodeCapacity(
                cpu_millicores_available=16_000,
                memory_bytes_available=12 * 2**30,
            ),
        ),
    )
    replica = ReplicaProfile(cpu_millicores=4_000, memory_bytes=8 * 2**30)
    profile = ResourceProfile(
        cpu_millicores=8_000,
        memory_bytes=16 * 2**30,
        replicas=(replica, replica),
    )

    assert capacity.fit(profile) is CapacityFit.INSUFFICIENT_CAPACITY


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


def test_insufficient_known_capacity_plus_an_unreadable_node_is_unreadable() -> None:
    capacity = _cpu_capacity(2_000, unreadable_node=True)

    assert capacity.fit(_two_eight_cpu_replicas()) is CapacityFit.REQUIRED_DIMENSION_UNREADABLE


def test_fully_readable_fragmentation_is_insufficient() -> None:
    assert (
        _cpu_capacity(5_700, 5_700, 5_700, 5_700, 5_700).fit(_two_eight_cpu_replicas())
        is CapacityFit.INSUFFICIENT_CAPACITY
    )


def test_known_sufficient_nodes_are_fit_even_with_an_unreadable_extra_node() -> None:
    capacity = _cpu_capacity(8_000, 8_000, unreadable_node=True)

    assert capacity.fit(_two_eight_cpu_replicas()) is CapacityFit.FIT


def test_malformed_status_node_does_not_turn_unknown_capacity_into_insufficient() -> None:
    status = {
        "cluster": {
            "inventory": {
                "available": {
                    "nodes": [
                        {"allocatable": {"cpu": 2_000}, "available": {"cpu": 2_000}},
                        {"name": "unreadable-node"},
                    ]
                }
            }
        }
    }

    assert (
        from_provider_status(status).fit(_two_eight_cpu_replicas())
        is CapacityFit.REQUIRED_DIMENSION_UNREADABLE
    )


def test_readable_status_nodes_can_prove_fit_despite_an_unreadable_extra_node() -> None:
    status = {
        "cluster": {
            "inventory": {
                "available": {
                    "nodes": [
                        {"allocatable": {"cpu": 8_000}, "available": {"cpu": 8_000}},
                        {"allocatable": {"cpu": 8_000}, "available": {"cpu": 8_000}},
                        {"name": "unreadable-node"},
                    ]
                }
            }
        }
    }

    assert from_provider_status(status).fit(_two_eight_cpu_replicas()) is CapacityFit.FIT


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


def test_independent_replica_effect_mutation_turns_12_2_2_green(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Restoring non-consuming per-replica checks recreates the packing defect."""
    source = Path(capacity_module.__file__).read_text()
    target = "        placement_fits = _replicas_fit_nodes(profile.replicas, readable_nodes)\n"
    replacement = """        placement_fits = all(
            any(node.fit(replica) is CapacityFit.FIT for node in readable_nodes)
            for replica in profile.replicas
        )
"""
    assert source.count(target) == 1
    mutated_source = source.replace(target, replacement)
    assert mutated_source.count(replacement) == 1

    module_name = "mutated_sum_only_capacity"
    module = _load_mutated_capacity(mutated_source, module_name, tmp_path, monkeypatch)

    replica = module.ReplicaProfile(cpu_millicores=8_000)
    profile = module.ResourceProfile(cpu_millicores=16_000, replicas=(replica, replica))
    nodes = tuple(
        module.NodeCapacity(cpu_millicores_available=value) for value in (12_000, 2_000, 2_000)
    )
    mutated_capacity = module.ProviderCapacity.from_totals(
        cpu=(16_000, 16_000), node_capacities=nodes
    )

    assert mutated_capacity.fit(profile) is module.CapacityFit.FIT


def test_unreadable_precedence_effect_mutation_turns_unknown_insufficient(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Deleting the unreadable precedence recreates the partial-sum verdict."""
    source = Path(capacity_module.__file__).read_text()
    target = """        if len(readable_nodes) != len(self.node_capacities):
            return CapacityFit.REQUIRED_DIMENSION_UNREADABLE
"""
    assert source.count(target) == 1
    mutated_source = source.replace(target, "")
    assert target not in mutated_source

    module_name = "mutated_unreadable_precedence"
    module = _load_mutated_capacity(mutated_source, module_name, tmp_path, monkeypatch)

    replica = module.ReplicaProfile(cpu_millicores=8_000)
    profile = module.ResourceProfile(cpu_millicores=16_000, replicas=(replica, replica))
    capacity = module.ProviderCapacity.from_totals(
        cpu=(2_000, 2_000),
        node_capacities=(
            module.NodeCapacity(cpu_millicores_available=2_000),
            module.NodeCapacity(),
        ),
    )

    assert capacity.fit(profile) is module.CapacityFit.INSUFFICIENT_CAPACITY
