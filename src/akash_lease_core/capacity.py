"""Provider capacity as an AVAILABLE FRACTION, for emptiest-first selection.

Motivating measurement (operator, 2026-08-25): the Lisbon datacenter is far
larger than its siblings and typically sits below 10% utilisation while the
others run near 50%. Cheapest-first ignores that entirely, so a three-region
deployment can pile onto providers that have no room while a mostly-empty one
sits idle. Emptiest-first raises the odds that all three regions place at once.

⚠ WHY A FRACTION AND NOT FREE UNITS. Absolute headroom is not comparable across
providers of different sizes, and it is not what determines whether a workload
fits *relative to contention*. The operator asked for percentage, and percentage
is also the only figure that means the same thing on a large and a small
provider.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass

__all__ = ["ProviderCapacity"]

_DIMENSIONS = ("cpu", "memory", "storage", "gpu")


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

    @classmethod
    def from_totals(cls, **dims: tuple[float, float] | None) -> ProviderCapacity:
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
                continue
            available, total = pair
            if not (math.isfinite(available) and math.isfinite(total)):
                raise ValueError(f"{name}: available and total must be finite")
            if total < 0 or available < 0:
                raise ValueError(f"{name}: available and total must be non-negative")
            if total == 0:
                out[name] = None
                continue
            out[name] = min(1.0, available / total)
        return cls(**out)

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


def _sum_nodes(nodes: object) -> dict[str, tuple[float, float]] | None:
    """Sum (available, total) per dimension across a provider's nodes."""
    if not isinstance(nodes, (list, tuple)) or not nodes:
        return None
    totals: dict[str, float] = {ours: 0.0 for ours, _ in _STATUS_DIMENSIONS}
    frees: dict[str, float] = {ours: 0.0 for ours, _ in _STATUS_DIMENSIONS}
    seen = False
    for node in nodes:
        if not isinstance(node, Mapping):
            continue
        allocatable = node.get("allocatable")
        available = node.get("available")
        if not isinstance(allocatable, Mapping) or not isinstance(available, Mapping):
            continue
        seen = True
        for ours, theirs in _STATUS_DIMENSIONS:
            total = allocatable.get(theirs)
            free = available.get(theirs)
            if isinstance(total, bool) or isinstance(free, bool):
                # ⚠ bool is an int subclass; True would silently count as 1 unit.
                continue
            if isinstance(total, (int, float)) and math.isfinite(total) and total >= 0:
                totals[ours] += float(total)
            if isinstance(free, (int, float)) and math.isfinite(free) and free >= 0:
                frees[ours] += float(free)
    if not seen:
        return None
    return {ours: (frees[ours], totals[ours]) for ours, _ in _STATUS_DIMENSIONS}


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
    pairs = _sum_nodes(available.get("nodes"))
    if pairs is None:
        return ProviderCapacity()
    return ProviderCapacity.from_totals(**pairs)
