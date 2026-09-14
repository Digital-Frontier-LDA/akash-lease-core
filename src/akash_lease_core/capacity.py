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
from decimal import Decimal, Inexact, Rounded, localcontext
from enum import Enum
from fractions import Fraction
from typing import TypeAlias

__all__ = [
    "PLACEMENT_SEARCH_STATE_LIMIT",
    "CapacityFit",
    "NodeCapacity",
    "ProviderCapacity",
    "ReplicaProfile",
    "ResourceProfile",
]

PLACEMENT_SEARCH_STATE_LIMIT = 100_000
_MAX_EXACT_FLOAT_INTEGER = 2**53
_Quantity: TypeAlias = int | float | Decimal

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
    PLACEMENT_SEARCH_UNSUPPORTED = "placement_search_unsupported"


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

    cpu_millicores_available: _Quantity | None = None
    memory_bytes_available: _Quantity | None = None
    storage_bytes_available: _Quantity | None = None
    gpu_count_available: _Quantity | None = None

    def __post_init__(self) -> None:
        for name in _AVAILABLE_FIELDS.values():
            value = getattr(self, name)
            if value is None:
                continue
            if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
                raise ValueError(f"{name} must be a non-negative real number or None")
            if not math.isfinite(value) or value < 0:
                raise ValueError(f"{name} must be finite and non-negative")

    def fit(self, profile: ReplicaProfile) -> CapacityFit:
        """Return whether this one node can hold this one replica."""

        if any(
            getattr(self, _AVAILABLE_FIELDS[dimension]) is None
            for dimension in profile.requested_dimensions
        ):
            return CapacityFit.REQUIRED_DIMENSION_UNREADABLE
        for dimension in profile.requested_dimensions:
            available = getattr(self, _AVAILABLE_FIELDS[dimension])
            if available is None:
                return CapacityFit.REQUIRED_DIMENSION_UNREADABLE
            exact_available = _exact_quantity(available)
            if exact_available is None:
                return CapacityFit.PLACEMENT_SEARCH_UNSUPPORTED
            if exact_available < getattr(profile, _REQUEST_FIELDS[dimension]):
                return CapacityFit.INSUFFICIENT_CAPACITY
        return CapacityFit.FIT


def _exact_quantity(value: _Quantity) -> Fraction | None:
    """Return exact arithmetic input, or ``None`` outside the supported float domain."""

    if isinstance(value, int):
        return Fraction(value)
    if isinstance(value, Decimal):
        return Fraction(value)
    if abs(value) >= _MAX_EXACT_FLOAT_INTEGER:
        return None
    return Fraction.from_float(value)


def _replicas_fit_nodes(
    replicas: tuple[ReplicaProfile, ...], nodes: tuple[NodeCapacity, ...]
) -> CapacityFit:
    """Return whether every replica can be placed while consuming node capacity.

    This is an exact, deterministic multidimensional bin-packing search within
    :data:`PLACEMENT_SEARCH_STATE_LIMIT`. Exhaustion is typed unsupported; it
    never becomes an approximate ``FIT`` or ``INSUFFICIENT_CAPACITY`` verdict.
    The iterative search is independent of Python's recursion limit.
    """

    if not nodes:
        return CapacityFit.INSUFFICIENT_CAPACITY

    dimensions = tuple(
        dimension
        for dimension in _DIMENSIONS
        if any(dimension in replica.requested_dimensions for replica in replicas)
    )
    exact_nodes: list[tuple[Fraction, ...]] = []
    for node in nodes:
        exact_node: list[Fraction] = []
        for dimension in dimensions:
            value = getattr(node, _AVAILABLE_FIELDS[dimension])
            if value is None:
                return CapacityFit.REQUIRED_DIMENSION_UNREADABLE
            exact = _exact_quantity(value)
            if exact is None:
                return CapacityFit.PLACEMENT_SEARCH_UNSUPPORTED
            exact_node.append(exact)
        exact_nodes.append(tuple(exact_node))
    maximum = {
        dimension: max(node[index] for node in exact_nodes)
        for index, dimension in enumerate(dimensions)
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
    initial_nodes = tuple(sorted(exact_nodes, reverse=True))
    initial_state = (0, initial_nodes)
    pending = [initial_state]
    seen = {initial_state}
    while pending:
        index, remaining = pending.pop()
        if index == len(ordered_replicas):
            return CapacityFit.FIT
        replica = ordered_replicas[index]
        exact_request = tuple(
            _exact_quantity(getattr(replica, _REQUEST_FIELDS[dimension]))
            for dimension in dimensions
        )
        if any(value is None for value in exact_request):
            return CapacityFit.PLACEMENT_SEARCH_UNSUPPORTED
        request = tuple(value for value in exact_request if value is not None)
        tried: set[tuple[Fraction, ...]] = set()
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
            state = (index + 1, next_remaining)
            if state not in seen:
                if len(seen) >= PLACEMENT_SEARCH_STATE_LIMIT:
                    return CapacityFit.PLACEMENT_SEARCH_UNSUPPORTED
                seen.add(state)
                pending.append(state)
    return CapacityFit.INSUFFICIENT_CAPACITY


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
    cpu_millicores_available: _Quantity | None = None
    memory_bytes_available: _Quantity | None = None
    storage_bytes_available: _Quantity | None = None
    gpu_count_available: _Quantity | None = None
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
            if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
                raise ValueError(f"{name} must be a non-negative real number or None")
            if not math.isfinite(value) or value < 0:
                raise ValueError(f"{name} must be finite and non-negative")
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
        **dims: tuple[_Quantity, _Quantity] | None,
    ) -> ProviderCapacity:
        """Build from ``dimension=(available, total)`` pairs.

        ⚠ ``total == 0`` yields ``None``, NOT ``0.0``. A provider that offers no
        GPUs at all is *not applicable* on that dimension; scoring it as 0% free
        would rank every CPU-only provider as completely full and hand every GPU
        provider the auction regardless of contention.

        ⚠ Without ``node_capacities`` each ``available`` is taken to be the provider's
        COMPLETE free total: :meth:`fit` reads an aggregate below the request as proven
        ``insufficient_capacity``. Pass a node list when the aggregate may omit nodes.
        """
        unknown = set(dims) - set(_DIMENSIONS)
        if unknown:
            raise ValueError(f"unknown dimension(s): {', '.join(sorted(unknown))}")
        out: dict[str, _Quantity | None] = {}
        for name, pair in dims.items():
            if pair is None:
                out[name] = None
                out[_AVAILABLE_FIELDS[name]] = None
                continue
            available, total = pair
            if any(
                isinstance(value, bool) or not isinstance(value, (int, float, Decimal))
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
            out[name] = min(1.0, float(Decimal(str(available)) / Decimal(str(total))))
            out[_AVAILABLE_FIELDS[name]] = available
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

        if any(
            getattr(self, dimension) is None or getattr(self, _AVAILABLE_FIELDS[dimension]) is None
            for dimension in profile.requested_dimensions
        ):
            return CapacityFit.REQUIRED_DIMENSION_UNREADABLE
        aggregate_fits = True
        for dimension in profile.requested_dimensions:
            available = getattr(self, _AVAILABLE_FIELDS[dimension])
            if available is None:
                return CapacityFit.REQUIRED_DIMENSION_UNREADABLE
            exact_available = _exact_quantity(available)
            if exact_available is None:
                return CapacityFit.PLACEMENT_SEARCH_UNSUPPORTED
            if exact_available < getattr(profile, _REQUEST_FIELDS[dimension]):
                aggregate_fits = False

        if self.node_capacities is None:
            # ⭐ The aggregate is the whole provider's free total here, so a request
            # above it cannot be placed by ANY distribution: that is proven, not
            # unreadable (#50 L4). A sufficient aggregate still proves nothing about
            # same-node fit, so that half stays unreadable.
            if not aggregate_fits:
                return CapacityFit.INSUFFICIENT_CAPACITY
            return CapacityFit.REQUIRED_DIMENSION_UNREADABLE
        if self._contradicts_nodes(profile):
            # ⛔ Evidence that disagrees with itself proves neither FIT nor
            # INSUFFICIENT_CAPACITY, in either direction (#50 L3).
            return CapacityFit.REQUIRED_DIMENSION_UNREADABLE
        readable_nodes = tuple(
            node
            for node in self.node_capacities
            if all(
                getattr(node, _AVAILABLE_FIELDS[dimension]) is not None
                for dimension in profile.requested_dimensions
            )
        )
        placement_fit = _replicas_fit_nodes(profile.replicas, readable_nodes)
        if aggregate_fits and placement_fit is CapacityFit.FIT:
            return CapacityFit.FIT
        if len(readable_nodes) != len(self.node_capacities):
            return CapacityFit.REQUIRED_DIMENSION_UNREADABLE
        if placement_fit is CapacityFit.PLACEMENT_SEARCH_UNSUPPORTED:
            return placement_fit
        return CapacityFit.INSUFFICIENT_CAPACITY

    def _contradicts_nodes(self, profile: ResourceProfile) -> bool:
        """Whether the aggregate and the node list cannot both be true.

        Nodes that report a dimension can only add up to AT MOST the aggregate:
        an aggregate may omit a node that cannot be read (the provider-status
        adapter sums only readable nodes), but never contain less than the nodes
        it summarises. When every node reports the dimension, the two must be
        equal. Anything else is a snapshot that disagrees with itself.
        """

        nodes = self.node_capacities or ()
        for dimension in profile.requested_dimensions:
            aggregate = getattr(self, _AVAILABLE_FIELDS[dimension])
            exact_aggregate = None if aggregate is None else _exact_quantity(aggregate)
            values = [getattr(node, _AVAILABLE_FIELDS[dimension]) for node in nodes]
            readable = [_exact_quantity(value) for value in values if value is not None]
            if exact_aggregate is None or any(value is None for value in readable):
                continue  # unsupported arithmetic is typed by the placement search
            node_sum = sum(value for value in readable if value is not None)
            if node_sum > exact_aggregate:
                return True
            if len(readable) == len(values) and node_sum != exact_aggregate:
                return True
        return False

    def available_fraction_for(self, profile: ResourceProfile) -> float | None:
        """Binding fraction only when fit and the ranking population are both proven."""

        if self.fit(profile) is not CapacityFit.FIT or not self.ranking_complete_for(profile):
            return None
        return min(getattr(self, dimension) for dimension in profile.requested_dimensions)

    def ranking_complete_for(self, profile: ResourceProfile) -> bool:
        """Whether every node reports every dimension used by the ranking.

        Readable nodes can prove that a replica population fits even when another
        node is unreadable. They cannot prove the provider-wide free fraction:
        silently omitting the unreadable node changes both the numerator and the
        denominator and can manufacture an ``emptiest`` winner.
        """

        return self.node_capacities is not None and all(
            all(
                getattr(node, _AVAILABLE_FIELDS[dimension]) is not None
                for dimension in profile.requested_dimensions
            )
            for node in self.node_capacities
        )

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

    ⚠ An ``int`` outside float range is UNREADABLE, never an exception (#52).
    ``json.loads`` parses a 401-digit integer without complaint; converting it
    to float for ``isfinite`` raised ``OverflowError`` out of the adapter. The
    quantity is dropped like any other unreadable one — clamping it to the
    float ceiling instead would let corrupt data prove a workload fits.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        return False
    try:
        return math.isfinite(value) and value >= 0
    except OverflowError:
        return False


def _exact_decimal_sum(values: list[Decimal]) -> Decimal:
    """Sum finite decimals with no rounding, however many digits they need.

    ⛔ The default context keeps 28 significant digits. Summing there rounds the
    aggregate while each node keeps its exact value, so the snapshot would disagree
    with itself and the aggregate/node contradiction check would read a healthy
    provider as unreadable (10**28 + 1 does it with integers). Precision grows until
    the sum is exact instead.
    """

    precision = 28
    while True:
        with localcontext() as context:
            context.prec = precision
            context.traps[Inexact] = True
            context.traps[Rounded] = True
            try:
                return sum(values, Decimal(0))
            except (Inexact, Rounded):
                precision *= 2


def _summarize_nodes(
    nodes: object,
) -> tuple[dict[str, tuple[float, float] | None], tuple[NodeCapacity, ...]] | None:
    """Retain each node's free units while summing provider-wide totals."""
    if not isinstance(nodes, (list, tuple)) or not nodes:
        return None
    total_parts: dict[str, list[Decimal]] = {ours: [] for ours, _ in _STATUS_DIMENSIONS}
    free_parts: dict[str, list[Decimal]] = {ours: [] for ours, _ in _STATUS_DIMENSIONS}
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
        node_free: dict[str, _Quantity] = {}
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
            exact_total = Decimal(total) if isinstance(total, int) else Decimal(str(total))
            exact_free = Decimal(free) if isinstance(free, int) else Decimal(str(free))
            total_parts[ours].append(exact_total)
            free_parts[ours].append(exact_free)
            node_free[_AVAILABLE_FIELDS[ours]] = (
                int(exact_free) if exact_free == exact_free.to_integral_value() else exact_free
            )
        node_capacities.append(NodeCapacity(**node_free))
    if not seen:
        return None
    # ⛔ AN AGGREGATE THAT OVERFLOWED IS NOT A MEASUREMENT EITHER. Two per-node
    #    values can each be finite (1e308) and sum to inf; from_totals() would then
    #    raise ValueError, breaking this module's documented promise that a
    #    malformed payload yields an unreadable capacity rather than an exception.
    #    Found by CodeRabbit on #26.
    totals = {ours: _exact_decimal_sum(parts) for ours, parts in total_parts.items()}
    frees = {ours: _exact_decimal_sum(parts) for ours, parts in free_parts.items()}
    for ours, _ in _STATUS_DIMENSIONS:
        if not math.isfinite(totals[ours]) or not math.isfinite(frees[ours]):
            return None
    return (
        {
            ours: None
            if ours in overreported
            else (
                int(frees[ours])
                if frees[ours] == frees[ours].to_integral_value()
                else frees[ours],
                int(totals[ours])
                if totals[ours] == totals[ours].to_integral_value()
                else totals[ours],
            )
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
