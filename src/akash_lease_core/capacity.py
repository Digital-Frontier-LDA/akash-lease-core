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
