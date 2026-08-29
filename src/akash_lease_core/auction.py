"""Clock-neutral provider bid auction semantics.

This module deliberately performs no I/O and reads no clock.  Adapters collect
bids from Console, chain RPC, or another transport, feed observations into an
``Auction``, and call :meth:`Auction.evaluate` with their monotonic time.

The contract is intentionally small:

* collect for the complete configured window (0--60 seconds),
* then choose the cheapest open preferred bid when one exists,
* otherwise choose the first observed open eligible fallback bid,
* never compare prices expressed in different denominations.

CRASH RESUME.  :meth:`Auction.snapshot` and :meth:`Auction.restore` carry the
whole of an auction's state through a plain ``dict``.  An adapter that dies
mid-window resumes with the arrival times it already paid for instead of
restarting the clock -- and, because ``observe`` keeps FIRST arrival, a restart
that lost them would not merely re-collect, it would re-date every surviving bid
to the restart and hand the fallback rule a pool that all arrived at once.

This lives HERE and not in a consumer for one reason that is not a preference:
field completeness is a property of WHERE THE CODE LIVES.  This module can
enumerate ``dataclasses.fields(BidObservation)``; a consumer cannot, because the
dataclass is defined in another repository and a field added here is invisible
there until a customer notices.  :meth:`Auction.observe`'s own comment records
that defect one layer down -- the hand-written rebuild it replaced *"passed 6 of
7 fields and lost ``proofs`` to its default of ``()``"*.  A hand-written
(de)serializer in a consumer is that same defect one layer up, on the path that
decides which provider gets paid.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping
from dataclasses import Field, dataclass, field, fields, replace
from decimal import Decimal, InvalidOperation
from enum import Enum
from typing import Any

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


# ── auction snapshot schema ─────────────────────────────────────────────────
#
# The schema version of the SNAPSHOT, deliberately distinct from
# ``AuctionPolicy.version``. The two answer different questions -- "how was this
# auction decided" versus "how was this auction written down" -- and one string
# doing both means a serialisation change cannot be shipped without claiming the
# policy also changed.
AUCTION_SNAPSHOT_VERSION = "auction-snapshot/v1"

#: Exactly the top-level keys of a snapshot at ``AUCTION_SNAPSHOT_VERSION``.
_SNAPSHOT_KEYS = frozenset({"version", "scope", "started_at", "policy", "bids"})


class UnsupportedSnapshotVersion(ValueError):
    """Raised when :meth:`Auction.restore` is handed a schema it does not know.

    ⛔ SEPARATE from a plain ``ValueError`` on purpose. "I cannot read this
    schema" is recoverable by the caller -- start a fresh auction, keep the
    blob for a later core -- while "this snapshot is malformed" is a defect.
    Collapsing them would force a consumer to parse an error message to tell a
    version skew from corruption.
    """


def _encode_identity(value: object) -> object:
    return value


def _encode_float(value: float) -> float:
    return float(value)


def _encode_float_or_none(value: float | None) -> float | None:
    return None if value is None else float(value)


def _encode_decimal(value: Decimal) -> str:
    # ⛔ str(), never float(). ``float(Decimal("0.1"))`` is a DIFFERENT number,
    # and this one decides which provider gets paid.
    return str(value)


def _encode_list(value: tuple[str, ...]) -> list[str]:
    return list(value)


def _encode_sorted(value: frozenset[str]) -> list[str]:
    # sorted(), not list(): a set has no order, so an unsorted dump would make
    # the same auction produce different bytes on different runs and defeat any
    # consumer that compares or deduplicates snapshots.
    return sorted(value)


def _encode_sorted_or_none(value: frozenset[str] | None) -> list[str] | None:
    return None if value is None else sorted(value)


def _encode_enum(value: Enum) -> object:
    return value.value


def _encode_capacity(value: ProviderCapacity | None) -> dict[str, object] | None:
    # ⛔ ``None`` capacity stays ``None``; it does NOT become an all-zero
    # ProviderCapacity and a readable dimension does NOT become 0.0. This
    # module's whole thesis is that None never means "full" and never means
    # "empty" -- see ProviderCapacity's own docstring. A serializer that emitted
    # 0.0 for an unmeasured dimension would make an unreadable provider sort
    # last on evidence nobody ever collected.
    return None if value is None else _encode_dataclass(value)


def _decode_str(value: object, where: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{where}: expected a string, got {type(value).__name__}")
    return value


def _decode_float(value: object, where: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{where}: expected a number, got {type(value).__name__}")
    if not math.isfinite(value):
        raise ValueError(f"{where}: expected a finite number, got {value!r}")
    return float(value)


def _decode_float_or_none(value: object, where: str) -> float | None:
    return None if value is None else _decode_float(value, where)


def _decode_int_or_none(value: object, where: str) -> int | None:
    if value is None:
        return None
    # A bool is an int in Python; a ``true`` in the blob would silently mean 1.
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{where}: expected an integer or null, got {type(value).__name__}")
    return value


def _decode_decimal(value: object, where: str) -> Decimal:
    if not isinstance(value, str):
        raise ValueError(
            f"{where}: expected a decimal STRING, got {type(value).__name__} -- a JSON "
            "number has already lost the value it was asked to carry"
        )
    try:
        return Decimal(value)
    except InvalidOperation as exc:
        raise ValueError(f"{where}: {value!r} is not a decimal number") from exc


def _decode_str_list(value: object, where: str) -> list[str]:
    if not isinstance(value, list):
        raise ValueError(f"{where}: expected a list, got {type(value).__name__}")
    for index, item in enumerate(value):
        if not isinstance(item, str):
            raise ValueError(f"{where}[{index}]: expected a string, got {type(item).__name__}")
    return value


def _decode_str_tuple(value: object, where: str) -> tuple[str, ...]:
    return tuple(_decode_str_list(value, where))


def _decode_str_frozenset(value: object, where: str) -> frozenset[str]:
    return frozenset(_decode_str_list(value, where))


def _decode_str_frozenset_or_none(value: object, where: str) -> frozenset[str] | None:
    # ⛔ ``None`` is NOT the empty set here. ``eligible_providers=None`` means NO
    # RESTRICTION; ``frozenset()`` is a policy under which every bid is rejected
    # ``provider_not_eligible``. Coercing one into the other on the way back in
    # would turn "everyone may bid" into "nobody may" across a restart.
    return None if value is None else _decode_str_frozenset(value, where)


def _decode_capacity(value: object, where: str) -> ProviderCapacity | None:
    if value is None:
        return None
    return ProviderCapacity(**_decode_dataclass(ProviderCapacity, value, where))


def _decode_preferred_selection(value: object, where: str) -> PreferredSelection:
    text = _decode_str(value, where)
    try:
        return PreferredSelection(text)
    except ValueError as exc:
        known = ", ".join(sorted(item.value for item in PreferredSelection))
        raise ValueError(f"{where}: unknown selection {text!r}; known: {known}") from exc


_Encode = Callable[[Any], object]
_Decode = Callable[[Any, str], object]

# ⭐ ONE table, keyed by the field's own ANNOTATION, driving BOTH directions.
#
# The alternative is a hand-written field list per dataclass, which is exactly
# the shape ``observe``'s comment records losing ``proofs``. Here a field added
# to BidObservation, AuctionPolicy or ProviderCapacity whose type is already in
# this table is carried automatically, and a field of a type that is NOT in it
# raises at snapshot time (see ``_codec_for``) instead of being dropped.
_CODECS: dict[str, tuple[_Encode, _Decode]] = {
    "str": (_encode_identity, _decode_str),
    "float": (_encode_float, _decode_float),
    "float | None": (_encode_float_or_none, _decode_float_or_none),
    "int | None": (_encode_identity, _decode_int_or_none),
    "Decimal": (_encode_decimal, _decode_decimal),
    "tuple[str, ...]": (_encode_list, _decode_str_tuple),
    "frozenset[str]": (_encode_sorted, _decode_str_frozenset),
    "frozenset[str] | None": (_encode_sorted_or_none, _decode_str_frozenset_or_none),
    "ProviderCapacity | None": (_encode_capacity, _decode_capacity),
    "PreferredSelection": (_encode_enum, _decode_preferred_selection),
}


def _codec_for(owner: type, spec: Field) -> tuple[_Encode, _Decode]:
    """The (encode, decode) pair for one dataclass field, or a loud refusal."""

    annotation = " ".join(str(spec.type).split())
    codec = _CODECS.get(annotation)
    if codec is None:
        raise TypeError(
            f"{owner.__name__}.{spec.name}: the auction snapshot schema has no codec for "
            f"the annotation {annotation!r}. Add one to _CODECS and bump "
            f"AUCTION_SNAPSHOT_VERSION -- a field this schema cannot write is a bid this "
            f"auction loses on the next restart, silently."
        )
    return codec


def _encode_dataclass(instance: Any) -> dict[str, object]:
    owner = type(instance)
    return {
        spec.name: _codec_for(owner, spec)[0](getattr(instance, spec.name))
        for spec in fields(instance)
    }


def _decode_dataclass(cls: Any, payload: object, where: str) -> dict[str, object]:
    if not isinstance(payload, Mapping):
        raise ValueError(f"{where}: expected a mapping, got {type(payload).__name__}")
    expected = {spec.name for spec in fields(cls)}
    present = set(payload)
    missing = sorted(expected - present)
    unknown = sorted(present - expected)
    if missing or unknown:
        detail = "".join(
            (
                f" missing {missing}." if missing else "",
                f" unknown {unknown}." if unknown else "",
            )
        )
        raise ValueError(
            f"{where}: does not match {cls.__name__} at schema {AUCTION_SNAPSHOT_VERSION}.{detail}"
        )
    return {
        spec.name: _codec_for(cls, spec)[1](payload[spec.name], f"{where}.{spec.name}")
        for spec in fields(cls)
    }


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

    def snapshot(self, *, scope: str | None = None) -> dict[str, object]:
        """Return this auction's whole state as a plain, JSON-native ``dict``.

        ⭐ A plain dict, and NOT a JSON string. The caller decides serialisation
        -- exactly as ``build_proxy_connect_message`` already does -- because a
        consumer that stores this in a JSONB column, a msgpack blob or a row per
        bid should not have to parse a string this module produced. This module
        therefore imports no ``json``, and a test asserts it.

        ⛔ ``observed_at`` IS RELATIVE TO ``started_at``, AND THAT IS THE WHOLE
        CLOCK CONTRACT. CPython documents ``time.monotonic``'s reference point as
        undefined -- *"only the difference between the results of two calls is
        valid"* -- so a raw reading means nothing in a new process. On Linux it is
        *coincidentally* meaningful on the same host, which is worse than
        meaningless, because it makes a same-host restart test PASS and a
        container reschedule produce nonsense. Build the auction with
        ``started_at=0``, supply ``observed_at`` as elapsed seconds since the
        auction began, and persist a WALL-CLOCK anchor of your own beside this
        dict; on resume compute ``now = (utcnow() - anchor).total_seconds()``.
        :meth:`restore` refuses a snapshot that did not follow this rule.

        ⚠ ``scope`` NAMES THE ORDER THIS AUCTION IS FOR, and this module never
        reads it. ``bid_key`` is unique WITHIN an order and NOT across the chain:
        a consumer measured ten live rows of ``bids/list`` collapsing to four
        keys across seven deployments, because ``provider/gseq/oseq/bseq`` names
        a different bid in every deployment that has one. One ``Auction`` is one
        order, so the keys are unambiguous HERE -- but a snapshot is a thing that
        outlives the process, gets stored next to its siblings and gets handed
        back by a lookup that can be wrong. Passing the order's identity (a
        ``dseq``, say) makes :meth:`restore` able to REFUSE the wrong one instead
        of merging two deployments' bids into one auction, which ``observe``
        cannot detect: it raises only when a key changes PROVIDER.

        :param scope: opaque caller-owned identity of the order this auction is
            for. If set, :meth:`restore` REQUIRES a matching ``expect_scope``.
        """

        if scope is not None and (not isinstance(scope, str) or not scope):
            raise ValueError("scope must be a non-empty string or None")
        return {
            "version": AUCTION_SNAPSHOT_VERSION,
            "scope": scope,
            "started_at": float(self.started_at),
            "policy": _encode_dataclass(self.policy),
            # Insertion order, so ``restore(snapshot(a)).snapshot()`` is the same
            # dict and not merely an equivalent one. ``evaluate`` sorts its own
            # candidates, so order does not change any verdict -- it only makes
            # the artifact comparable byte for byte.
            "bids": [_encode_dataclass(item) for item in self._latest_by_key.values()],
        }

    @classmethod
    def restore(
        cls,
        snapshot: Mapping[str, object],
        *,
        expect_scope: str | None = None,
        rebase_started_at: float | None = None,
    ) -> Auction:
        """Rebuild an ``Auction`` from :meth:`snapshot`, or refuse to.

        Everything below is a REFUSAL, not a fallback. A resumed auction decides
        which provider gets paid; a best-effort parse of a blob this module does
        not understand is the one outcome worse than not resuming at all.

        * **An unknown schema version raises** :class:`UnsupportedSnapshotVersion`
          -- the same discipline ``parse_result_exit_code(strict=True)`` applies
          to a frame it cannot read.
        * **A field set that does not match the dataclass raises**, naming what
          is missing and what is unknown. The sets come from
          ``dataclasses.fields``, so adding a field to ``BidObservation`` here
          fails the suite instead of arriving as its default in a consumer.
        * **A price that is not a string raises.** ``float`` is not a lossless
          carrier for ``Decimal`` and never was.
        * **A ``started_at`` that is not 0 raises** unless ``rebase_started_at``
          is given explicitly -- see :meth:`snapshot` for why a raw monotonic
          reading is not a time.
        * **A duplicate ``bid_key`` raises.** Two rows under one key is a blob
          two auctions were written into; ``observe`` would silently keep one.
        * **A ``scope`` mismatch raises**, in both directions: a snapshot that
          names its order restored by a caller that does not say which order it
          expects is refused just as loudly as a disagreement.

        :param expect_scope: the order identity the caller believes it is
            resuming. Must equal the snapshot's ``scope``; ``None`` matches only
            a snapshot that carries no scope.
        :param rebase_started_at: re-anchor the timeline. Every ``observed_at``
            shifts by the same delta, so relative arrival is preserved exactly.
        """

        if not isinstance(snapshot, Mapping):
            raise ValueError(f"snapshot: expected a mapping, got {type(snapshot).__name__}")

        version = snapshot.get("version")
        if version != AUCTION_SNAPSHOT_VERSION:
            raise UnsupportedSnapshotVersion(
                f"snapshot.version: cannot read {version!r}; this core writes and reads "
                f"{AUCTION_SNAPSHOT_VERSION!r} only"
            )

        present = set(snapshot)
        missing = sorted(_SNAPSHOT_KEYS - present)
        unknown = sorted(present - _SNAPSHOT_KEYS)
        if missing or unknown:
            detail = "".join(
                (
                    f" missing {missing}." if missing else "",
                    f" unknown {unknown}." if unknown else "",
                )
            )
            raise ValueError(f"snapshot: wrong top-level keys.{detail}")

        scope = snapshot["scope"]
        if scope is not None and not isinstance(scope, str):
            raise ValueError(
                f"snapshot.scope: expected a string or null, got {type(scope).__name__}"
            )
        if scope != expect_scope:
            raise ValueError(
                f"snapshot.scope: this snapshot is for {scope!r} and the caller expected "
                f"{expect_scope!r}. bid_key is unique within ONE order, so restoring "
                "another order's snapshot merges two deployments' bids into one auction "
                "and observe() cannot see it."
            )

        started_at = _decode_float(snapshot["started_at"], "snapshot.started_at")
        if rebase_started_at is None:
            if started_at != 0:
                raise ValueError(
                    f"snapshot.started_at: expected 0, got {started_at!r}. A snapshot whose "
                    "clock is not relative holds raw monotonic readings, whose reference "
                    "point CPython leaves undefined across processes. Pass "
                    "rebase_started_at= to re-anchor it deliberately."
                )
            delta = 0.0
            new_started_at = 0.0
        else:
            new_started_at = _decode_float(rebase_started_at, "rebase_started_at")
            delta = new_started_at - started_at

        policy = AuctionPolicy(**_decode_dataclass(AuctionPolicy, snapshot["policy"], "policy"))

        raw_bids = snapshot["bids"]
        if not isinstance(raw_bids, list):
            raise ValueError(f"snapshot.bids: expected a list, got {type(raw_bids).__name__}")
        observations: dict[str, BidObservation] = {}
        for index, raw in enumerate(raw_bids):
            where = f"bids[{index}]"
            values = _decode_dataclass(BidObservation, raw, where)
            if delta:
                values["observed_at"] = values["observed_at"] + delta  # type: ignore[operator]
            observation = BidObservation(**values)  # type: ignore[arg-type]
            if observation.bid_key in observations:
                raise ValueError(
                    f"{where}: duplicate bid_key {observation.bid_key!r}. An Auction holds one "
                    "observation per key; two here means two auctions were written into one "
                    "snapshot, and keeping either silently would lose a real bid."
                )
            observations[observation.bid_key] = observation

        auction = cls(policy, started_at=new_started_at)
        auction._latest_by_key = observations
        return auction

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
