"""Clock-neutral provider bid auction semantics.

This module deliberately performs no I/O and reads no clock.  Adapters collect
bids from Console, chain RPC, or another transport, feed observations into an
``Auction``, and call :meth:`Auction.evaluate` with their monotonic time.

The contract is intentionally small:

* collect for the complete configured window (0--60 seconds),
* then choose the cheapest open preferred bid when one exists,
* otherwise choose the first observed open eligible fallback bid,
* never compare prices expressed in different denominations.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum


class AuctionStatus(str, Enum):
    """State returned by :meth:`Auction.evaluate`."""

    COLLECTING = "collecting"
    DECIDED = "decided"
    EXPIRED = "expired"


class MixedBidDenominations(ValueError):
    """Raised when a candidate pool contains currencies that cannot be compared."""


@dataclass(frozen=True, slots=True)
class BidObservation:
    """One normalized provider bid as observed by an I/O adapter."""

    bid_key: str
    provider: str
    price: Decimal
    denom: str
    observed_at: float
    state: str = "open"

    def __post_init__(self) -> None:
        if not self.bid_key:
            raise ValueError("bid_key must not be empty")
        if not self.provider:
            raise ValueError("provider must not be empty")
        if not self.denom:
            raise ValueError("denom must not be empty")
        price = self.price if isinstance(self.price, Decimal) else Decimal(str(self.price))
        if not price.is_finite() or price < 0:
            raise ValueError("price must be a finite non-negative number")
        object.__setattr__(self, "price", price)
        if not math.isfinite(self.observed_at):
            raise ValueError("observed_at must be finite")


@dataclass(frozen=True, slots=True)
class AuctionPolicy:
    """A snapshotted provider policy for one auction."""

    collection_window_seconds: float = 60
    fallback_window_seconds: float = 60
    preferred_providers: frozenset[str] = field(default_factory=frozenset)
    eligible_providers: frozenset[str] | None = None
    excluded_providers: frozenset[str] = field(default_factory=frozenset)
    version: str = "provider-auction/v2"

    def __post_init__(self) -> None:
        if not math.isfinite(self.collection_window_seconds):
            raise ValueError("collection_window_seconds must be finite")
        if not 0 <= self.collection_window_seconds <= 60:
            raise ValueError("collection_window_seconds must be between 0 and 60 inclusive")
        if not math.isfinite(self.fallback_window_seconds):
            raise ValueError("fallback_window_seconds must be finite")
        if not 0 <= self.fallback_window_seconds <= 120:
            raise ValueError("fallback_window_seconds must be between 0 and 120 inclusive")
        object.__setattr__(self, "preferred_providers", frozenset(self.preferred_providers))
        if self.eligible_providers is not None:
            object.__setattr__(self, "eligible_providers", frozenset(self.eligible_providers))
        object.__setattr__(self, "excluded_providers", frozenset(self.excluded_providers))
        if not self.version:
            raise ValueError("version must not be empty")


@dataclass(frozen=True, slots=True)
class RejectedBid:
    """Machine-readable reason an observed bid could not participate."""

    bid_key: str
    provider: str
    reason: str


@dataclass(frozen=True, slots=True)
class AuctionResult:
    """Current or terminal auction evaluation."""

    status: AuctionStatus
    policy_version: str
    started_at: float
    deadline: float
    fallback_deadline: float
    evaluated_at: float
    selected: BidObservation | None
    selection_reason: str
    considered: tuple[BidObservation, ...] = ()
    rejected: tuple[RejectedBid, ...] = ()


class Auction:
    """Accumulate normalized bid state and make one deadline-bound decision."""

    def __init__(self, policy: AuctionPolicy, *, started_at: float) -> None:
        if not math.isfinite(started_at):
            raise ValueError("started_at must be finite")
        self.policy = policy
        self.started_at = started_at
        self.deadline = started_at + policy.collection_window_seconds
        self.fallback_deadline = self.deadline + policy.fallback_window_seconds
        self._latest_by_key: dict[str, BidObservation] = {}

    def observe(self, observation: BidObservation) -> None:
        """Record the newest state for a stable adapter-supplied bid key."""

        current = self._latest_by_key.get(observation.bid_key)
        if current is not None and current.provider != observation.provider:
            raise ValueError(
                f"bid_key {observation.bid_key!r} changed provider "
                f"from {current.provider!r} to {observation.provider!r}"
            )
        if current is None or observation.observed_at >= current.observed_at:
            self._latest_by_key[observation.bid_key] = observation

    def observe_many(
        self, observations: list[BidObservation] | tuple[BidObservation, ...]
    ) -> None:
        """Record several observations in adapter order."""

        for observation in observations:
            self.observe(observation)

    def evaluate(self, *, now: float) -> AuctionResult:
        """Return collection state or the deterministic terminal decision."""

        if not math.isfinite(now):
            raise ValueError("now must be finite")
        if now < self.deadline:
            return self._result(
                status=AuctionStatus.COLLECTING,
                now=now,
                selected=None,
                reason="collection_window_open",
            )

        candidates: list[BidObservation] = []
        rejected: list[RejectedBid] = []
        for observation in sorted(
            self._latest_by_key.values(), key=lambda item: (item.provider, item.bid_key)
        ):
            if observation.state.lower() != "open":
                rejected.append(self._reject(observation, "bid_not_open"))
            elif observation.provider in self.policy.excluded_providers:
                rejected.append(self._reject(observation, "provider_excluded"))
            elif (
                self.policy.eligible_providers is not None
                and observation.provider not in self.policy.eligible_providers
            ):
                rejected.append(self._reject(observation, "provider_not_eligible"))
            else:
                candidates.append(observation)

        if not candidates and now < self.fallback_deadline:
            return self._result(
                status=AuctionStatus.COLLECTING,
                now=now,
                selected=None,
                reason="waiting_for_first_eligible_fallback",
                rejected=tuple(rejected),
            )

        if not candidates:
            return self._result(
                status=AuctionStatus.EXPIRED,
                now=now,
                selected=None,
                reason="no_eligible_open_bids",
                rejected=tuple(rejected),
            )

        preferred = [
            observation
            for observation in candidates
            if observation.provider in self.policy.preferred_providers
        ]
        pool = preferred or candidates
        denominations = sorted({observation.denom for observation in pool})
        if len(denominations) > 1:
            raise MixedBidDenominations(
                "cannot compare bid prices with different denominations: "
                + ", ".join(denominations)
            )

        if preferred:
            selected = min(pool, key=lambda item: (item.price, item.provider, item.bid_key))
            reason = "cheapest_preferred"
            considered = tuple(
                sorted(pool, key=lambda item: (item.price, item.provider, item.bid_key))
            )
        else:
            selected = min(pool, key=lambda item: (item.observed_at, item.provider, item.bid_key))
            reason = "first_eligible_fallback"
            considered = tuple(
                sorted(pool, key=lambda item: (item.observed_at, item.provider, item.bid_key))
            )
        return self._result(
            status=AuctionStatus.DECIDED,
            now=now,
            selected=selected,
            reason=reason,
            considered=considered,
            rejected=tuple(rejected),
        )

    @staticmethod
    def _reject(observation: BidObservation, reason: str) -> RejectedBid:
        return RejectedBid(
            bid_key=observation.bid_key,
            provider=observation.provider,
            reason=reason,
        )

    def _result(
        self,
        *,
        status: AuctionStatus,
        now: float,
        selected: BidObservation | None,
        reason: str,
        considered: tuple[BidObservation, ...] = (),
        rejected: tuple[RejectedBid, ...] = (),
    ) -> AuctionResult:
        return AuctionResult(
            status=status,
            policy_version=self.policy.version,
            started_at=self.started_at,
            deadline=self.deadline,
            fallback_deadline=self.fallback_deadline,
            evaluated_at=now,
            selected=selected,
            selection_reason=reason,
            considered=considered,
            rejected=rejected,
        )
