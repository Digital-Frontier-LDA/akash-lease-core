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
from dataclasses import dataclass, field, replace
from decimal import Decimal
from enum import Enum

from .capacity import ProviderCapacity


class PreferredSelection(str, Enum):
    """How to choose AMONG preferred bids once the collection window closes.

    ⚠ This changes only the choice among PREFERRED bids. The window semantics,
    the fallback rule, and the eligibility filters are untouched -- an emptiest
    policy that also relaxed eligibility would be two changes wearing one name.
    """

    CHEAPEST = "cheapest"
    #: Highest AVAILABLE FRACTION on the binding dimension. Motivated by a
    #: measured fleet imbalance: one datacenter runs <10% utilised while its
    #: siblings run ~50%, so cheapest-first crowds the busy providers and a
    #: three-region placement fails on the ones with no room.
    EMPTIEST = "emptiest"


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
    proofs: tuple[str, ...] = ()
    #: Provider headroom at observation time. ``None`` = not measured,
    #: which is distinct from "measured as full" -- see ProviderCapacity.
    capacity: ProviderCapacity | None = None
    #: The order GROUP this bid is for.
    #:
    #: ⛔ ``None`` means NOT SUPPLIED, and it must never be read as 1.
    #: just-akash#195: `_cheapest_bid` picks a bid across ALL groups and the
    #: leasing path then leases `gseq=1` regardless -- so a winning bid for
    #: group 7 is leased as group 1 and the lease FAILS. Selecting a bid
    #: without carrying its group is what makes that mismatch EXPRESSIBLE.
    #:
    #: Measured there: splitting an order into groups roughly DOUBLES the bid
    #: rate (74.9% of 191 vs 36.6% of 303), because a provider that can satisfy
    #: some of twelve resources cannot bid at all when they are one indivisible
    #: group. That gain is unreachable until the winner names its own group.
    gseq: int | None = None

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
        if self.gseq is not None:
            # A bool is an int in Python; `gseq=True` would silently mean group 1.
            if isinstance(self.gseq, bool) or not isinstance(self.gseq, int):
                raise ValueError("gseq must be an int or None (None = not supplied)")
            if self.gseq < 1:
                # Akash groups are 1-based. A 0 here is the shape a missing value
                # takes when someone defaults an absent field to zero.
                raise ValueError("gseq must be >= 1 -- Akash group sequences are 1-based")
        object.__setattr__(self, "proofs", tuple(self.proofs))


@dataclass(frozen=True, slots=True)
class AuctionPolicy:
    """A snapshotted provider policy for one auction."""

    collection_window_seconds: float = 60
    fallback_window_seconds: float = 60
    preferred_providers: frozenset[str] = field(default_factory=frozenset)
    eligible_providers: frozenset[str] | None = None
    excluded_providers: frozenset[str] = field(default_factory=frozenset)
    required_proofs: frozenset[str] = field(default_factory=frozenset)
    preferred_selection: PreferredSelection = PreferredSelection.CHEAPEST
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
        object.__setattr__(self, "required_proofs", frozenset(self.required_proofs))
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
    missing_required_proofs: tuple[str, ...] = ()


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
        """Record a `BidObservation`, preserving first arrival, refreshing mutable state.

        ⛔ FIRST-ARRIVAL IS IMMUTABLE. A bid re-observed on a later poll has not
        "arrived" twice; overwriting its `observed_at` with the later poll's time
        would erase the ordering the FALLBACK RULE depends on. With every bid in
        the candidate pool carrying the last poll's timestamp, "first observed"
        degenerates into a tie-break on (provider, bid_key) and the fallback
        selection chooses by last index, not by arrival.

        ⇒ Mutable fields (price, state) ARE refreshed on later observations,
        because a bid can legitimately change (open → closed) while arrival
        cannot. BidObservation is frozen, so refreshing mutable fields means
        rebuilding the instance with the FIRST `observed_at` preserved.

        Matches `console_api_backend.poll_bids`'s `first_seen` shape exactly —
        keyed on the adapter-supplied `bid_key` (typically `provider/gseq/oseq`),
        first arrival kept, other fields refreshed. PR #1586's `BidAuction.observe_raw`
        carries the same guard on the raw-polling path; once both consumers pin
        this version, the adapter-level guard can be dropped.
        """
        current = self._latest_by_key.get(observation.bid_key)
        if current is None:
            self._latest_by_key[observation.bid_key] = observation
            return
        if current.provider != observation.provider:
            raise ValueError(
                f"bid_key {observation.bid_key!r} changed provider "
                f"from {current.provider!r} to {observation.provider!r}"
            )
        # Re-observation: KEEP first arrival, REFRESH mutable state.
        # `replace` rather than a field-by-field rebuild: this is FIELD-COMPLETE by
        # construction, so a field added to BidObservation later cannot be silently
        # dropped here. The hand-written rebuild this replaces passed 6 of 7 fields and
        # lost `proofs` to its default of () on every re-observation — the same defect
        # class as the one this method exists to fix, one field over.
        #
        # observed_at is the ONLY field taken from the stored copy: it is the bid's
        # arrival, not its latest sighting. Everything else (price, state, proofs) is
        # mutable and the newest observation is authoritative.
        self._latest_by_key[observation.bid_key] = replace(
            observation, observed_at=current.observed_at
        )

    def observe_many(
        self, observations: list[BidObservation] | tuple[BidObservation, ...]
    ) -> None:
        """Record several observations in adapter order."""

        for observation in observations:
            self.observe(observation)

    def evaluate(
        self,
        *,
        now: float,
        already_selected: frozenset[str] | None = None,
    ) -> AuctionResult:
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
            elif observation.observed_at > min(now, self.fallback_deadline):
                rejected.append(self._reject(observation, "bid_observed_after_fallback_deadline"))
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
            and observation.observed_at <= self.deadline
        ]
        pool = preferred or candidates
        denominations = sorted({observation.denom for observation in pool})
        if len(denominations) > 1:
            raise MixedBidDenominations(
                "cannot compare bid prices with different denominations: "
                + ", ".join(denominations)
            )

        if preferred:
            emptiest = self.policy.preferred_selection is PreferredSelection.EMPTIEST
            readable = [
                item for item in pool if item.capacity is not None and item.capacity.is_readable
            ]
            if emptiest and readable:
                # ⭐ ANTI-AFFINITY FIRST, headroom second.
                #
                # A multi-region deployment evaluates several auctions against ONE
                # capacity snapshot. Without this term all of them see the same
                # emptiest provider and all of them choose it -- a thundering herd
                # that is strictly WORSE than cheapest-first, which at least has a
                # stable price tiebreak. Emptiest-first only delivers "room in all
                # three" if a provider already taken this round steps aside.
                #
                # ⚠ It DEPRIORITISES, it does not exclude. If the already-chosen
                # provider is the only preferred bidder, taking it beats failing
                # to place -- so this changes the ORDER, never the eligibility.
                taken = already_selected or frozenset()

                def rank(item: BidObservation) -> tuple:
                    frac = item.capacity.available_fraction()  # type: ignore[union-attr]
                    return (item.provider in taken, -frac, item.price, item.provider, item.bid_key)

                selected = min(readable, key=rank)
                reason = "emptiest_preferred"
                considered = tuple(sorted(readable, key=rank))
            else:
                selected = min(pool, key=lambda item: (item.price, item.provider, item.bid_key))
                # ⛔ A degraded selection MUST NOT report as the mode that was
                # asked for. Silently returning "cheapest_preferred" here would
                # make an unmeasurable fleet indistinguishable from a fleet that
                # was measured and happened to agree.
                reason = (
                    "emptiest_unavailable_fell_back_to_cheapest"
                    if emptiest
                    else "cheapest_preferred"
                )
                considered = tuple(
                    sorted(pool, key=lambda item: (item.price, item.provider, item.bid_key))
                )
        else:
            selected = min(pool, key=lambda item: (item.observed_at, item.provider, item.bid_key))
            reason = "first_eligible_fallback"
            considered = tuple(
                sorted(pool, key=lambda item: (item.observed_at, item.provider, item.bid_key))
            )
        missing = tuple(sorted(set(self.policy.required_proofs) - set(selected.proofs)))
        return self._result(
            status=AuctionStatus.DECIDED,
            now=now,
            selected=selected,
            reason=reason,
            considered=considered,
            rejected=tuple(rejected),
            missing_required_proofs=missing,
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
        missing_required_proofs: tuple[str, ...] = (),
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
            missing_required_proofs=missing_required_proofs,
        )
