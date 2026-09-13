"""Provider capacity and exact workload demand for emptiest-first selection.

Motivating measurement (operator, 2026-08-25): the Lisbon datacenter is far
larger than its siblings and typically sits below 10% utilisation while the
others run near 50%. Cheapest-first ignores that entirely, so a three-region
deployment can pile onto providers that have no room while a mostly-empty one
sits idle. Emptiest-first raises the odds that all three regions place at once.

Fractions rank providers of different sizes. Absolute free units prove that the
exact group fits before a fraction is allowed to rank it: the aggregate must fit
and all replicas must pack onto nodes while consuming their multidimensional
headroom. Neither proof substitutes for the other, so provider snapshots retain
both.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from functools import cache

__all__ = ["CapacityFit", "NodeCapacity", "ProviderCapacity", "ReplicaProfile", "ResourceProfile"]

_DIMENSIONS = ("cpu", "memory", "storage", "gpu")
_AVAILABLE_FIELDS = {
    "cpu": "cpu_millicores_available",
    "memory": "memory_bytes_available",
    "storage": "storage_bytes_available",
    "gpu": "gpu_count_available",
}
_REQUEST_FIELDS = {
    "cpu": "cpu_millicores",
    "memory": "memory_bytes",
    "storage": "storage_bytes",
    "gpu": "gpu_count",
}


class CapacityFit(str, Enum):
    """Typed outcome of comparing one provider with one exact group request."""

    FIT = "fit"
    REQUIRED_DIMENSION_UNREADABLE = "required_capacity_unreadable"
    INSUFFICIENT_CAPACITY = "insufficient_capacity"


@dataclass(frozen=True, slots=True)
class ReplicaProfile:
    """Resources one replica of one service must fit on the same node."""

    cpu_millicores: int = 0
    memory_bytes: int = 0
    storage_bytes: int = 0
    gpu_count: int = 0

    def __post_init__(self) -> None:
        for name in _REQUEST_FIELDS.values():
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")
        if not self.requested_dimensions:
            raise ValueError("replica profile must request at least one resource dimension")

    @property
    def requested_dimensions(self) -> tuple[str, ...]:
        return tuple(
            dimension for dimension in _DIMENSIONS if getattr(self, _REQUEST_FIELDS[dimension]) > 0
        )


@dataclass(frozen=True, slots=True)
class ResourceProfile:
    """Aggregate resources requested by the exact Akash group behind a bid.

    Values use explicit Akash/provider-status units. Consumers must multiply
    each service by its ``count`` and sum the final submitted group. Passing a
    per-replica shape in the aggregate fields would make a provider appear to
    fit a population it cannot actually host. ``replicas`` carries each
    replica shape, including repeated shapes for service ``count``, so node fit
    and aggregate fit can both be proved. Omitting it means the group contains
    one replica with the aggregate shape.
    """

    cpu_millicores: int = 0
    memory_bytes: int = 0
    storage_bytes: int = 0
    gpu_count: int = 0
    replicas: tuple[ReplicaProfile, ...] = ()

    def __post_init__(self) -> None:
        for name in _REQUEST_FIELDS.values():
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")
        if not self.requested_dimensions:
            raise ValueError("resource profile must request at least one resource dimension")
        if not isinstance(self.replicas, tuple) or any(
            not isinstance(replica, ReplicaProfile) for replica in self.replicas
        ):
            raise ValueError("replicas must be a tuple of ReplicaProfile values")
        if not self.replicas:
            object.__setattr__(
                self,
                "replicas",
                (
                    ReplicaProfile(
                        **{name: getattr(self, name) for name in _REQUEST_FIELDS.values()}
                    ),
                ),
            )
        for name in _REQUEST_FIELDS.values():
            replica_total = sum(getattr(replica, name) for replica in self.replicas)
            if replica_total != getattr(self, name):
                raise ValueError(
                    f"replicas sum to {replica_total} {name}, "
                    f"but the aggregate requests {getattr(self, name)}"
                )

    @property
    def requested_dimensions(self) -> tuple[str, ...]:
        return tuple(
            dimension for dimension in _DIMENSIONS if getattr(self, _REQUEST_FIELDS[dimension]) > 0
        )


@dataclass(frozen=True, slots=True)
class NodeCapacity:
    """Absolute free resources on one schedulable provider node."""

    cpu_millicores_available: float | None = None
    memory_bytes_available: float | None = None
    storage_bytes_available: float | None = None
    gpu_count_available: float | None = None

    def __post_init__(self) -> None:
        for name in _AVAILABLE_FIELDS.values():
            value = getattr(self, name)
            if value is None:
                continue
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError(f"{name} must be a non-negative real number or None")
            if not math.isfinite(value) or value < 0:
                raise ValueError(f"{name} must be finite and non-negative")
            object.__setattr__(self, name, float(value))

    def fit(self, profile: ReplicaProfile) -> CapacityFit:
        """Return whether this one node can hold this one replica."""

        for dimension in profile.requested_dimensions:
            available = getattr(self, _AVAILABLE_FIELDS[dimension])
            if available is None:
                return CapacityFit.REQUIRED_DIMENSION_UNREADABLE
            if available < getattr(profile, _REQUEST_FIELDS[dimension]):
                return CapacityFit.INSUFFICIENT_CAPACITY
        return CapacityFit.FIT


def _replicas_fit_nodes(
    replicas: tuple[ReplicaProfile, ...], nodes: tuple[NodeCapacity, ...]
) -> bool:
    """Return whether every replica can be placed while consuming node capacity.

    This is an exact, deterministic multidimensional bin-packing search. Replica
    order is only a search heuristic; memoization and canonical node states mean
    the verdict does not depend on provider node order or SDL service order.
    """

    if not nodes:
        return False

    dimensions = tuple(_AVAILABLE_FIELDS)
    maximum = {
        dimension: max(getattr(node, _AVAILABLE_FIELDS[dimension]) or 0.0 for node in nodes)
        for dimension in dimensions
    }

    def difficulty(replica: ReplicaProfile) -> tuple[float, ...]:
        ratios = tuple(
            getattr(replica, _REQUEST_FIELDS[dimension]) / maximum[dimension]
            if maximum[dimension]
            else 0.0
            for dimension in dimensions
        )
        return (max(ratios), sum(ratios), *ratios)

    ordered_replicas = tuple(sorted(replicas, key=difficulty, reverse=True))
    initial_nodes = tuple(
        sorted(
            (
                tuple(
                    float(getattr(node, _AVAILABLE_FIELDS[dimension]) or 0.0)
                    for dimension in dimensions
                )
                for node in nodes
            ),
            reverse=True,
        )
    )

    @cache
    def place(index: int, remaining: tuple[tuple[float, ...], ...]) -> bool:
        if index == len(ordered_replicas):
            return True
        replica = ordered_replicas[index]
        request = tuple(
            float(getattr(replica, _REQUEST_FIELDS[dimension])) for dimension in dimensions
        )
        tried: set[tuple[float, ...]] = set()
        for node_index, available in enumerate(remaining):
            if available in tried or any(
                have < need for have, need in zip(available, request, strict=True)
            ):
                continue
            tried.add(available)
            after = tuple(have - need for have, need in zip(available, request, strict=True))
            next_remaining = tuple(
                sorted(
                    (*remaining[:node_index], after, *remaining[node_index + 1 :]), reverse=True
                )
            )
            if place(index + 1, next_remaining):
                return True
        return False

    return place(0, initial_nodes)


@dataclass(frozen=True, slots=True)
class ProviderCapacity:
    """Available fraction per dimension, in ``[0.0, 1.0]``. ``None`` = UNREADABLE.

    ⛔ ``None`` NEVER means "full" and never means "empty". A dimension nobody
    could read must not be rendered as a number, for the same reason
    ``OrderObservation.lease_count`` is ``int | None``: a silent 0.0 would make
    an unmeasured provider look maximally contended and an unmeasured fleet look
    uniformly so.
    """

    cpu: float | None = None
    memory: float | None = None
    storage: float | None = None
    gpu: float | None = None
    cpu_millicores_available: float | None = None
    memory_bytes_available: float | None = None
    storage_bytes_available: float | None = None
    gpu_count_available: float | None = None
    node_capacities: tuple[NodeCapacity, ...] | None = None

    def __post_init__(self) -> None:
        for name in _DIMENSIONS:
            value = getattr(self, name)
            if value is None:
                continue
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError(f"{name} must be a real number or None")
            # ⛔ NaN passes every comparison, so a NaN dimension would neither
            # trip a bounds check nor lose a min() -- it would silently become
            # the binding dimension and decide the auction.
            if not math.isfinite(value):
                raise ValueError(f"{name} must be finite -- NaN/inf would win min() silently")
            if not 0.0 <= value <= 1.0:
                raise ValueError(f"{name} must be a fraction in [0,1], got {value}")
            object.__setattr__(self, name, float(value))
        for name in _AVAILABLE_FIELDS.values():
            value = getattr(self, name)
            if value is None:
                continue
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError(f"{name} must be a non-negative real number or None")
            if not math.isfinite(value) or value < 0:
                raise ValueError(f"{name} must be finite and non-negative")
            object.__setattr__(self, name, float(value))
        if self.node_capacities is not None and (
            not isinstance(self.node_capacities, tuple)
            or not self.node_capacities
            or any(not isinstance(node, NodeCapacity) for node in self.node_capacities)
        ):
            raise ValueError("node_capacities must be a non-empty tuple of NodeCapacity or None")

    @classmethod
    def from_totals(
        cls,
        *,
        node_capacities: tuple[NodeCapacity, ...] | None = None,
        **dims: tuple[float, float] | None,
    ) -> ProviderCapacity:
        """Build from ``dimension=(available, total)`` pairs.

        ⚠ ``total == 0`` yields ``None``, NOT ``0.0``. A provider that offers no
        GPUs at all is *not applicable* on that dimension; scoring it as 0% free
        would rank every CPU-only provider as completely full and hand every GPU
        provider the auction regardless of contention.
        """
        unknown = set(dims) - set(_DIMENSIONS)
        if unknown:
            raise ValueError(f"unknown dimension(s): {', '.join(sorted(unknown))}")
        out: dict[str, float | None] = {}
        for name, pair in dims.items():
            if pair is None:
                out[name] = None
                out[_AVAILABLE_FIELDS[name]] = None
                continue
            available, total = pair
            if any(
                isinstance(value, bool) or not isinstance(value, (int, float))
                for value in (available, total)
            ):
                raise ValueError(f"{name}: available and total must be real numbers")
            if not (math.isfinite(available) and math.isfinite(total)):
                raise ValueError(f"{name}: available and total must be finite")
            if total < 0 or available < 0:
                raise ValueError(f"{name}: available and total must be non-negative")
            if available > total:
                raise ValueError(
                    f"{name}: available must not exceed total ({available} > {total})"
                )
            if total == 0:
                out[name] = None
                out[_AVAILABLE_FIELDS[name]] = None
                continue
            out[name] = min(1.0, available / total)
            out[_AVAILABLE_FIELDS[name]] = float(available)
        return cls(**out, node_capacities=node_capacities)

    def available_fraction(self) -> float | None:
        """The BINDING dimension's available fraction, or ``None`` if unreadable.

        ⭐ The minimum, deliberately. A provider 90% free on CPU and 5% free on
        memory cannot take a workload needing memory, so the maximum -- or an
        average -- would recommend exactly the provider that will refuse the bid.
        Emptiest-first is only useful if "empty" means "empty where it binds".
        """
        readable = [
            value for value in (getattr(self, name) for name in _DIMENSIONS) if value is not None
        ]
        return min(readable) if readable else None

    def fit(self, profile: ResourceProfile) -> CapacityFit:
        """Prove both full-group aggregate fit and same-node replica fit."""

        aggregate_fits = True
        for dimension in profile.requested_dimensions:
            fraction = getattr(self, dimension)
            available = getattr(self, _AVAILABLE_FIELDS[dimension])
            if fraction is None or available is None:
                return CapacityFit.REQUIRED_DIMENSION_UNREADABLE
            if available < getattr(profile, _REQUEST_FIELDS[dimension]):
                aggregate_fits = False

        if self.node_capacities is None:
            return CapacityFit.REQUIRED_DIMENSION_UNREADABLE
        readable_nodes = tuple(
            node
            for node in self.node_capacities
            if all(
                getattr(node, _AVAILABLE_FIELDS[dimension]) is not None
                for dimension in profile.requested_dimensions
            )
        )
        placement_fits = _replicas_fit_nodes(profile.replicas, readable_nodes)
        if aggregate_fits and placement_fits:
            return CapacityFit.FIT
        if len(readable_nodes) != len(self.node_capacities):
            return CapacityFit.REQUIRED_DIMENSION_UNREADABLE
        return CapacityFit.INSUFFICIENT_CAPACITY

    def available_fraction_for(self, profile: ResourceProfile) -> float | None:
        """Binding fraction across requested dimensions, after a positive fit proof."""

        if self.fit(profile) is not CapacityFit.FIT:
            return None
        return min(getattr(self, dimension) for dimension in profile.requested_dimensions)

    @property
    def is_readable(self) -> bool:
        return self.available_fraction() is not None


# ── provider /status adapter ────────────────────────────────────────────────
#
# The dimension names a provider reports are NOT the names this module uses.
# `storage_ephemeral` is what a node advertises; `storage` is what
# ``ProviderCapacity`` calls it. Keeping the map in one place is the whole point
# of this adapter: every consumer that fetched `/status` itself would otherwise
# re-derive it, and a decision must come from the primitive rather than be
# re-derived per repo.
_STATUS_DIMENSIONS: tuple[tuple[str, str], ...] = (
    ("cpu", "cpu"),
    ("memory", "memory"),
    ("storage", "storage_ephemeral"),
    ("gpu", "gpu"),
)


def _usable(value: object) -> bool:
    """A node-reported quantity we are willing to add.

    ⚠ ``bool`` is an ``int`` subclass and must be rejected before the numeric
    check, or ``True`` contributes one unit of CPU.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    return math.isfinite(value) and value >= 0


def _summarize_nodes(
    nodes: object,
) -> tuple[dict[str, tuple[float, float] | None], tuple[NodeCapacity, ...]] | None:
    """Retain each node's free units while summing provider-wide totals."""
    if not isinstance(nodes, (list, tuple)) or not nodes:
        return None
    totals: dict[str, float] = {ours: 0.0 for ours, _ in _STATUS_DIMENSIONS}
    frees: dict[str, float] = {ours: 0.0 for ours, _ in _STATUS_DIMENSIONS}
    overreported: set[str] = set()
    node_capacities: list[NodeCapacity] = []
    seen = False
    for node in nodes:
        if not isinstance(node, Mapping):
            node_capacities.append(NodeCapacity())
            continue
        allocatable = node.get("allocatable")
        available = node.get("available")
        if not isinstance(allocatable, Mapping) or not isinstance(available, Mapping):
            node_capacities.append(NodeCapacity())
            continue
        seen = True
        node_free: dict[str, float] = {}
        for ours, theirs in _STATUS_DIMENSIONS:
            total = allocatable.get(theirs)
            free = available.get(theirs)
            # ⛔⛔ THE PAIR IS ATOMIC. Accepting one half and dropping the other is
            #     how corrupt data becomes a MEASUREMENT. Guarding each value
            #     separately -- the first version of this -- meant
            #     {"allocatable": {"cpu": 100}, "available": {"cpu": True}} added
            #     100 to the total and 0 to the free, yielding cpu == 0.0:
            #     "measured, and completely full". That is the one value this
            #     module exists to never emit from unreadable input, and it would
            #     have sorted a healthy provider last on evidence that was garbage.
            #     Found by CodeRabbit on #26; the bool test that was supposed to
            #     cover it set BOTH halves to True, so the symmetric case passed
            #     and the asymmetric one was never asked.
            # ⚠ bool is an int subclass, so it is excluded explicitly rather than
            #   by isinstance(x, (int, float)) -- True would count as 1 unit.
            if not _usable(total) or not _usable(free):
                continue
            # Available > allocatable is not "extra headroom". Clamping its
            # fraction to 1 while retaining the oversized absolute value would
            # let corrupt evidence prove a workload fits.
            if free > total:
                overreported.add(ours)
                continue
            totals[ours] += float(total)
            frees[ours] += float(free)
            node_free[_AVAILABLE_FIELDS[ours]] = float(free)
        node_capacities.append(NodeCapacity(**node_free))
    if not seen:
        return None
    # ⛔ AN AGGREGATE THAT OVERFLOWED IS NOT A MEASUREMENT EITHER. Two per-node
    #    values can each be finite (1e308) and sum to inf; from_totals() would then
    #    raise ValueError, breaking this module's documented promise that a
    #    malformed payload yields an unreadable capacity rather than an exception.
    #    Found by CodeRabbit on #26.
    for ours, _ in _STATUS_DIMENSIONS:
        if not math.isfinite(totals[ours]) or not math.isfinite(frees[ours]):
            return None
    return (
        {
            ours: None if ours in overreported else (frees[ours], totals[ours])
            for ours, _ in _STATUS_DIMENSIONS
        },
        tuple(node_capacities),
    )


def from_provider_status(status: object) -> ProviderCapacity:
    """Build a :class:`ProviderCapacity` from a provider's ``/status`` payload.

    An Akash provider serves free capacity at ``{host_uri}/status``, under::

        cluster.inventory.available.nodes[].allocatable   # total
        cluster.inventory.available.nodes[].available     # free

    This is the SANS-I/O half: the caller fetches, this parses. Keeping the fetch
    out means the library stays testable against fixtures and carries no HTTP,
    TLS or retry policy — the caller already owns those decisions.

    ⛔ A PAYLOAD THIS CANNOT READ YIELDS AN UNREADABLE CAPACITY, NOT A FULL ONE.
    Every failure below — absent inventory, empty node list, a schema that moved,
    a node whose numbers are strings — returns a capacity whose
    ``available_fraction()`` is ``None``. It never returns ``0.0``. The two are
    opposite instructions to the auction: ``None`` means *do not rank this
    provider*, while ``0.0`` means *measured, and completely full*, which sorts it
    last on evidence it does not have. Under emptiest selection a provider behind
    a flaky endpoint would then be permanently deprioritised for being unreachable
    rather than for being busy.

    ⚠ Consequently this does NOT raise on a malformed payload. Callers that need
    to distinguish "unreadable" from "empty" should check
    :attr:`ProviderCapacity.is_readable` — which is exactly what
    ``Auction.evaluate`` already does before it ranks anything.

    ``gpu`` needs no special case: a CPU-only provider reports ``0`` allocatable
    GPUs, ``from_totals`` maps a zero total to ``None``, and the dimension drops
    out of the binding minimum instead of scoring the provider 0% free.
    """
    if not isinstance(status, Mapping):
        return ProviderCapacity()
    cluster = status.get("cluster")
    if not isinstance(cluster, Mapping):
        return ProviderCapacity()
    inventory = cluster.get("inventory")
    if not isinstance(inventory, Mapping):
        return ProviderCapacity()
    available = inventory.get("available")
    if not isinstance(available, Mapping):
        return ProviderCapacity()
    summary = _summarize_nodes(available.get("nodes"))
    if summary is None:
        return ProviderCapacity()
    pairs, node_capacities = summary
    return ProviderCapacity.from_totals(node_capacities=node_capacities, **pairs)
