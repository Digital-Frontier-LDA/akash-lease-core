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
    PLACEMENT_SEARCH_STATE_LIMIT,
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


def test_1200_replicas_fit_without_using_python_recursion() -> None:
    replica = ReplicaProfile(cpu_millicores=1)
    profile = ResourceProfile(cpu_millicores=1_200, replicas=(replica,) * 1_200)

    assert _cpu_capacity(1_200).fit(profile) is CapacityFit.FIT


def test_1201_replicas_remain_an_exact_insufficient_control() -> None:
    replica = ReplicaProfile(cpu_millicores=1)
    profile = ResourceProfile(cpu_millicores=1_201, replicas=(replica,) * 1_201)

    assert _cpu_capacity(1_200).fit(profile) is CapacityFit.INSUFFICIENT_CAPACITY


def test_search_budget_exhaustion_is_typed_unsupported(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert PLACEMENT_SEARCH_STATE_LIMIT > 1
    monkeypatch.setattr(capacity_module, "PLACEMENT_SEARCH_STATE_LIMIT", 1)
    profile = ResourceProfile(cpu_millicores=1)

    assert _cpu_capacity(1).fit(profile) is CapacityFit.PLACEMENT_SEARCH_UNSUPPORTED


def test_search_budget_exhaustion_is_an_auction_rejection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(capacity_module, "PLACEMENT_SEARCH_STATE_LIMIT", 1)
    auction = Auction(
        AuctionPolicy(
            collection_window_seconds=1,
            fallback_window_seconds=0,
            preferred_providers=frozenset({"bounded"}),
            preferred_selection=PreferredSelection.EMPTIEST,
        ),
        started_at=0,
    )
    auction.observe(
        BidObservation(
            bid_key="bounded/1/1/1",
            provider="bounded",
            price=Decimal("1"),
            denom="uakt",
            observed_at=0,
            capacity=_cpu_capacity(1),
            resource_profile=ResourceProfile(cpu_millicores=1),
            gseq=1,
        )
    )

    result = auction.evaluate(now=1)

    assert result.status is AuctionStatus.EXPIRED
    assert [(item.provider, item.reason) for item in result.rejected] == [
        ("bounded", BidRejectionReason.PLACEMENT_SEARCH_UNSUPPORTED)
    ]


def test_integer_units_above_float_precision_remain_exact() -> None:
    request = 2**53 + 1
    replica = ReplicaProfile(cpu_millicores=request)
    profile = ResourceProfile(cpu_millicores=2 * request, replicas=(replica, replica))
    capacity = ProviderCapacity.from_totals(
        cpu=(2 * request + 2, 2 * request + 2),
        node_capacities=(
            NodeCapacity(cpu_millicores_available=2 * request - 1),
            NodeCapacity(cpu_millicores_available=3),
        ),
    )

    assert capacity.fit(profile) is CapacityFit.INSUFFICIENT_CAPACITY


def test_exact_large_integer_boundary_still_fits() -> None:
    request = 2**53 + 1
    replica = ReplicaProfile(cpu_millicores=request)
    profile = ResourceProfile(cpu_millicores=2 * request, replicas=(replica, replica))
    capacity = ProviderCapacity.from_totals(
        cpu=(2 * request, 2 * request),
        node_capacities=(NodeCapacity(cpu_millicores_available=2 * request),),
    )

    assert capacity.fit(profile) is CapacityFit.FIT


def test_large_float_capacity_is_typed_unsupported_instead_of_rounded_fit() -> None:
    request = 2**53 + 1
    profile = ResourceProfile(cpu_millicores=request)
    capacity = ProviderCapacity.from_totals(
        cpu=(float(request), float(request)),
        node_capacities=(NodeCapacity(cpu_millicores_available=float(request)),),
    )

    assert capacity.fit(profile) is CapacityFit.PLACEMENT_SEARCH_UNSUPPORTED


def test_decimal_capacity_above_float_precision_remains_exact() -> None:
    request = 2**53 + 1
    replica = ReplicaProfile(cpu_millicores=request)
    profile = ResourceProfile(cpu_millicores=2 * request, replicas=(replica, replica))
    capacity = ProviderCapacity.from_totals(
        cpu=(Decimal(2 * request + 2), Decimal(2 * request + 2)),
        node_capacities=(
            NodeCapacity(cpu_millicores_available=Decimal(2 * request - 1)),
            NodeCapacity(cpu_millicores_available=Decimal(3)),
        ),
    )

    assert capacity.fit(profile) is CapacityFit.INSUFFICIENT_CAPACITY


def test_decimal_capacity_remains_exact_through_auction_snapshot() -> None:
    request = 2**53 + 1
    profile = ResourceProfile(cpu_millicores=request)
    auction = Auction(
        AuctionPolicy(
            collection_window_seconds=1,
            fallback_window_seconds=0,
            preferred_providers=frozenset({"decimal"}),
            preferred_selection=PreferredSelection.EMPTIEST,
        ),
        started_at=0,
    )
    auction.observe(
        BidObservation(
            bid_key="decimal/1/1/1",
            provider="decimal",
            price=Decimal("1"),
            denom="uakt",
            observed_at=0,
            capacity=ProviderCapacity.from_totals(
                cpu=(Decimal(request - 1), Decimal(request)),
                node_capacities=(NodeCapacity(cpu_millicores_available=Decimal(request - 1)),),
            ),
            resource_profile=profile,
            gseq=1,
        )
    )

    restored = Auction.restore(json.loads(json.dumps(auction.snapshot())))
    result = restored.evaluate(now=1)

    assert result.status is AuctionStatus.EXPIRED
    assert result.rejected[0].reason is BidRejectionReason.INSUFFICIENT_CAPACITY


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
    target = "        placement_fit = _replicas_fit_nodes(profile.replicas, readable_nodes)\n"
    replacement = """        placement_fit = CapacityFit.FIT if all(
            any(node.fit(replica) is CapacityFit.FIT for node in readable_nodes)
            for replica in profile.replicas
        ) else CapacityFit.INSUFFICIENT_CAPACITY
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


def test_integer_to_float_effect_mutation_recreates_false_fit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = Path(capacity_module.__file__).read_text()
    target = """    if isinstance(value, int):
        return Fraction(value)
"""
    replacement = """    if isinstance(value, int):
        return Fraction(float(value))
"""
    assert source.count(target) == 1
    module = _load_mutated_capacity(
        source.replace(target, replacement), "mutated_float_quantity", tmp_path, monkeypatch
    )
    request = 2**53 + 1
    replica = module.ReplicaProfile(cpu_millicores=request)
    profile = module.ResourceProfile(cpu_millicores=2 * request, replicas=(replica, replica))
    capacity = module.ProviderCapacity.from_totals(
        cpu=(2 * request + 2, 2 * request + 2),
        node_capacities=(
            module.NodeCapacity(cpu_millicores_available=2 * request - 1),
            module.NodeCapacity(cpu_millicores_available=3),
        ),
    )

    assert capacity.fit(profile) is module.CapacityFit.FIT


def test_budget_result_effect_mutation_can_turn_exhaustion_into_insufficient(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = Path(capacity_module.__file__).read_text()
    target = """                if len(seen) >= PLACEMENT_SEARCH_STATE_LIMIT:
                    return CapacityFit.PLACEMENT_SEARCH_UNSUPPORTED
"""
    replacement = """                if len(seen) >= PLACEMENT_SEARCH_STATE_LIMIT:
                    return CapacityFit.INSUFFICIENT_CAPACITY
"""
    assert source.count(target) == 1
    module = _load_mutated_capacity(
        source.replace(target, replacement), "mutated_budget_result", tmp_path, monkeypatch
    )
    monkeypatch.setattr(module, "PLACEMENT_SEARCH_STATE_LIMIT", 1)
    profile = module.ResourceProfile(cpu_millicores=1)
    capacity = module.ProviderCapacity.from_totals(
        cpu=(1, 1), node_capacities=(module.NodeCapacity(cpu_millicores_available=1),)
    )

    assert capacity.fit(profile) is module.CapacityFit.INSUFFICIENT_CAPACITY


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
