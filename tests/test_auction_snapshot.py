"""Crash resume: ``Auction.snapshot()`` / ``Auction.restore()``.

WHY THIS FILE EXISTS, and why the field-completeness tests below enumerate
``dataclasses.fields`` instead of listing names.

``Auction.observe``'s own comment records the defect this whole module is
defending against, one layer down: the hand-written rebuild it replaced
*"passed 6 of 7 fields and lost ``proofs`` to its default of ``()`` on every
re-observation."* Nothing failed. No exception, no log line -- a bid simply
arrived at the selection rule with an empty proof set, and
``missing_required_proofs`` then said the winner was unqualified.

A serializer is the same shape of code with the same failure mode, and it runs
on the path that decides which provider gets PAID. So:

* the encoder walks ``dataclasses.fields`` and is field-complete by
  construction -- there is no list to fall out of date;
* these tests assert the key set AND the field COUNT, so a field added to
  ``BidObservation`` cannot ship without someone looking at the schema version;
* every refusal below is asserted to raise, because a resumed auction that
  best-effort-parses a blob it does not understand is worse than one that
  refuses to resume.
"""

from __future__ import annotations

import copy
import json
import pathlib
from dataclasses import fields
from decimal import Decimal

import pytest

from akash_lease_core.auction import (
    AUCTION_SNAPSHOT_VERSION,
    Auction,
    AuctionPolicy,
    AuctionStatus,
    BidObservation,
    PreferredSelection,
    UnsupportedSnapshotVersion,
    _codec_for,
)
from akash_lease_core.capacity import ProviderCapacity

# The dataclass shapes this schema version was written against. See the module
# docstring: these are asserted, not documented, because a count in prose is a
# claim nobody re-checks.
BID_OBSERVATION_FIELDS = 9
AUCTION_POLICY_FIELDS = 8
PROVIDER_CAPACITY_FIELDS = 4


def _policy(**overrides: object) -> AuctionPolicy:
    base: dict[str, object] = {
        "collection_window_seconds": 45,
        "fallback_window_seconds": 90,
        "preferred_providers": frozenset({"akash1lisbon", "akash1sofia"}),
        "eligible_providers": frozenset({"akash1lisbon", "akash1sofia", "akash1backup"}),
        "excluded_providers": frozenset({"akash1blocked"}),
        "required_proofs": frozenset({"gpu-attested"}),
        "preferred_selection": PreferredSelection.CHEAPEST,
        "version": "provider-auction/v2",
    }
    base.update(overrides)
    return AuctionPolicy(**base)  # type: ignore[arg-type]


def _bid(
    provider: str,
    price: str,
    observed_at: float,
    *,
    bid_key: str | None = None,
    state: str = "open",
    denom: str = "uact",
    proofs: tuple[str, ...] = (),
    capacity: ProviderCapacity | None = None,
    gseq: int | None = None,
) -> BidObservation:
    return BidObservation(
        bid_key=bid_key or f"{provider}/1/1/0",
        provider=provider,
        price=Decimal(price),
        denom=denom,
        observed_at=observed_at,
        state=state,
        proofs=proofs,
        capacity=capacity,
        gseq=gseq,
    )


def _populated() -> Auction:
    """An auction exercising every non-default field the schema can carry."""

    auction = Auction(_policy(), started_at=0)
    auction.observe(
        _bid(
            "akash1lisbon",
            "4.2",
            3.5,
            proofs=("gpu-attested", "audited"),
            capacity=ProviderCapacity(cpu=0.9, memory=0.5, storage=None, gpu=0.25),
            gseq=7,
        )
    )
    auction.observe(_bid("akash1sofia", "3.9", 9.25, capacity=ProviderCapacity()))
    auction.observe(_bid("akash1backup", "1.0", 1.0, state="closed"))
    auction.observe(_bid("akash1blocked", "0.5", 0.5, gseq=1))
    return auction


# ---------------------------------------------------------------------------
# 1. Round trip
# ---------------------------------------------------------------------------
class TestRoundTrip:
    def test_a_restored_auction_snapshots_to_the_identical_dict(self):
        original = _populated()
        first = original.snapshot()
        second = Auction.restore(first).snapshot()

        assert second == first

    def test_the_restored_auction_holds_the_same_observations_in_the_same_order(self):
        original = _populated()
        restored = Auction.restore(original.snapshot())

        assert list(restored._latest_by_key.items()) == list(original._latest_by_key.items())

    def test_the_restored_auction_reconstructs_policy_and_both_deadlines(self):
        original = _populated()
        restored = Auction.restore(original.snapshot())

        assert restored.policy == original.policy
        assert restored.started_at == original.started_at
        assert restored.deadline == original.deadline
        assert restored.fallback_deadline == original.fallback_deadline

    def test_an_auction_that_saw_no_bids_round_trips(self):
        empty = Auction(_policy(), started_at=0)
        restored = Auction.restore(empty.snapshot())

        assert restored.snapshot() == empty.snapshot()
        assert restored._latest_by_key == {}

    def test_the_snapshot_is_json_native_but_snapshot_returns_a_dict(self):
        """Property 6: the CALLER decides serialisation.

        ``build_proxy_connect_message`` already works this way. A consumer
        storing this in a JSONB column, a msgpack blob or a row per bid should
        not have to re-parse a string this module produced.
        """
        snap = _populated().snapshot()

        assert isinstance(snap, dict)
        assert Auction.restore(json.loads(json.dumps(snap))).snapshot() == snap

    def test_the_auction_module_imports_no_json(self):
        source = (
            pathlib.Path(__file__).parent.parent / "src" / "akash_lease_core" / "auction.py"
        ).read_text(encoding="utf-8")

        assert "import json" not in source

    def test_the_snapshot_is_byte_stable_across_runs(self):
        """Sets have no order, so an unsorted dump would make one auction
        produce two different artifacts and defeat any consumer that compares
        or deduplicates them."""
        assert json.dumps(_populated().snapshot()) == json.dumps(_populated().snapshot())


# ---------------------------------------------------------------------------
# 2. Field completeness — the reason this code is upstream and not downstream
# ---------------------------------------------------------------------------
class TestFieldCompleteness:
    """Enumerated from ``dataclasses.fields``, never from a written list.

    This is the whole argument for contributing this upstream rather than
    writing it in a consumer: only the repository that OWNS ``BidObservation``
    can enumerate its fields. A consumer's serializer cannot notice a field
    added here until a customer does.
    """

    def test_every_bid_observation_field_is_written_to_the_snapshot(self):
        snap = _populated().snapshot()
        declared = {spec.name for spec in fields(BidObservation)}

        for index, encoded in enumerate(snap["bids"]):
            assert set(encoded) == declared, f"bids[{index}] does not match BidObservation"

    def test_every_auction_policy_field_is_written_to_the_snapshot(self):
        snap = _populated().snapshot()

        assert set(snap["policy"]) == {spec.name for spec in fields(AuctionPolicy)}

    def test_every_provider_capacity_field_is_written_to_the_snapshot(self):
        snap = _populated().snapshot()
        encoded = next(item for item in snap["bids"] if item["capacity"] is not None)

        assert set(encoded["capacity"]) == {spec.name for spec in fields(ProviderCapacity)}

    def test_the_field_counts_this_schema_version_was_written_against(self):
        """Deliberately a COUNT, and deliberately brittle.

        Adding a field to ``BidObservation`` must not be a silent act. The key-set
        tests above keep passing when a field is added, because the encoder walks
        ``fields()`` -- which is the point, the field is carried. This test is the
        one that stops and asks whether ``AUCTION_SNAPSHOT_VERSION`` should move
        and whether the new field's TYPE has a codec.
        """
        assert len(fields(BidObservation)) == BID_OBSERVATION_FIELDS
        assert len(fields(AuctionPolicy)) == AUCTION_POLICY_FIELDS
        assert len(fields(ProviderCapacity)) == PROVIDER_CAPACITY_FIELDS

    def test_proofs_survives_the_round_trip(self):
        """PR #22's incident by name: ``proofs`` is the field the hand-written
        rebuild lost, and losing it here makes a qualified winner report as
        missing its required proofs."""
        restored = Auction.restore(_populated().snapshot())
        lisbon = restored._latest_by_key["akash1lisbon/1/1/0"]

        assert lisbon.proofs == ("gpu-attested", "audited")

    def test_gseq_survives_the_round_trip_and_none_stays_none(self):
        """just-akash#195: a winner that loses its group is leased as group 1
        and the lease FAILS. ``None`` is NOT SUPPLIED and must not become 1."""
        restored = Auction.restore(_populated().snapshot())

        assert restored._latest_by_key["akash1lisbon/1/1/0"].gseq == 7
        assert restored._latest_by_key["akash1sofia/1/1/0"].gseq is None

    def test_a_field_whose_type_has_no_codec_raises_instead_of_being_dropped(self):
        """The guard that fires the day someone adds a field of a new type.

        There is no such field today -- by construction, since the encoder is
        driven by this table -- so the guard is exercised against a fabricated
        annotation rather than a real one. Without it, an un-encodable field
        would have to be handled by a fallback, and every fallback here is a
        bid quietly lost on the next restart.
        """
        spec = next(item for item in fields(BidObservation) if item.name == "price")
        forged = copy.copy(spec)
        forged.type = "SomeTypeAddedLater"

        with pytest.raises(TypeError, match="no codec for the annotation"):
            _codec_for(BidObservation, forged)


# ---------------------------------------------------------------------------
# 3. Decimal is a string, never a float
# ---------------------------------------------------------------------------
class TestDecimalIsCarriedAsAString:
    def test_a_price_below_float_resolution_survives_exactly(self):
        tiny = Decimal("0.000000000000000001")
        auction = Auction(_policy(preferred_providers=frozenset()), started_at=0)
        auction.observe(_bid("akash1lisbon", "0.000000000000000001", 1.0))

        restored = Auction.restore(auction.snapshot())
        price = restored._latest_by_key["akash1lisbon/1/1/0"].price

        assert price == tiny
        # as_tuple(), not ==: it compares sign, digits AND exponent, so a value
        # that came back numerically equal but re-scaled would still fail.
        assert price.as_tuple() == tiny.as_tuple()

    def test_a_price_a_float_could_not_have_carried_survives(self):
        """The control that makes the string rule load-bearing rather than
        stylistic: this value is *gone* the moment it becomes a float."""
        precise = Decimal("1.000000000000000000000001")
        assert float(precise) == 1.0, "the premise of this test no longer holds"

        auction = Auction(_policy(preferred_providers=frozenset()), started_at=0)
        auction.observe(_bid("akash1lisbon", "1.000000000000000000000001", 1.0))

        restored = Auction.restore(auction.snapshot())

        assert restored._latest_by_key["akash1lisbon/1/1/0"].price == precise

    def test_the_encoded_price_is_a_string_and_not_a_number(self):
        snap = _populated().snapshot()

        for encoded in snap["bids"]:
            assert isinstance(encoded["price"], str)

    def test_trailing_zeroes_are_preserved_because_they_are_precision(self):
        auction = Auction(_policy(), started_at=0)
        auction.observe(_bid("akash1lisbon", "4.20", 1.0))

        restored = Auction.restore(auction.snapshot())

        assert str(restored._latest_by_key["akash1lisbon/1/1/0"].price) == "4.20"

    def test_a_numeric_price_in_the_blob_is_refused(self):
        """``float(Decimal("0.1"))`` is a different number. Accepting one here
        would let a lossy writer look correct until the two prices it cannot
        distinguish decide an auction."""
        snap = _populated().snapshot()
        snap["bids"][0]["price"] = 4.2

        with pytest.raises(ValueError, match="decimal STRING"):
            Auction.restore(snap)

    def test_a_price_that_is_not_a_number_at_all_is_refused(self):
        snap = _populated().snapshot()
        snap["bids"][0]["price"] = "four point two"

        with pytest.raises(ValueError, match="not a decimal number"):
            Auction.restore(snap)


# ---------------------------------------------------------------------------
# 4. None is not zero, and None is not empty
# ---------------------------------------------------------------------------
class TestNoneNeverBecomesAValue:
    def test_an_unreadable_dimension_round_trips_as_none(self):
        restored = Auction.restore(_populated().snapshot())
        capacity = restored._latest_by_key["akash1lisbon/1/1/0"].capacity

        assert capacity == ProviderCapacity(cpu=0.9, memory=0.5, storage=None, gpu=0.25)
        assert capacity.storage is None
        assert capacity.storage != 0.0

    def test_an_absent_capacity_stays_absent_and_does_not_become_all_zero(self):
        restored = Auction.restore(_populated().snapshot())

        assert restored._latest_by_key["akash1backup/1/1/0"].capacity is None

    def test_an_absent_capacity_and_a_wholly_unreadable_one_stay_distinct(self):
        """``capacity=None`` (never measured) and ``ProviderCapacity()`` (asked,
        every dimension unreadable) are different facts, and only the first is
        indistinguishable from "no adapter ran"."""
        restored = Auction.restore(_populated().snapshot())

        assert restored._latest_by_key["akash1backup/1/1/0"].capacity is None
        assert restored._latest_by_key["akash1sofia/1/1/0"].capacity == ProviderCapacity()
        assert restored._latest_by_key["akash1sofia/1/1/0"].capacity is not None

    def test_eligible_providers_none_means_no_restriction_and_survives_as_none(self):
        """``None`` is NO RESTRICTION; ``frozenset()`` is a policy under which
        every bid is rejected ``provider_not_eligible``. From the outcome alone
        the second is indistinguishable from a market that stopped bidding."""
        auction = Auction(_policy(eligible_providers=None), started_at=0)

        assert auction.snapshot()["policy"]["eligible_providers"] is None
        assert Auction.restore(auction.snapshot()).policy.eligible_providers is None

    def test_eligible_providers_empty_set_survives_as_an_empty_set(self):
        auction = Auction(_policy(eligible_providers=frozenset()), started_at=0)

        assert auction.snapshot()["policy"]["eligible_providers"] == []
        assert Auction.restore(auction.snapshot()).policy.eligible_providers == frozenset()


# ---------------------------------------------------------------------------
# 5. The schema version, and every other refusal
# ---------------------------------------------------------------------------
class TestRefusals:
    def test_the_snapshot_version_is_distinct_from_the_policy_version(self):
        snap = _populated().snapshot()

        assert snap["version"] == AUCTION_SNAPSHOT_VERSION
        assert snap["version"] != snap["policy"]["version"]

    def test_an_unknown_schema_version_raises_rather_than_being_parsed(self):
        snap = _populated().snapshot()
        snap["version"] = "auction-snapshot/v99"

        with pytest.raises(UnsupportedSnapshotVersion):
            Auction.restore(snap)

    def test_a_missing_schema_version_raises(self):
        snap = _populated().snapshot()
        del snap["version"]

        with pytest.raises(UnsupportedSnapshotVersion):
            Auction.restore(snap)

    def test_version_skew_is_catchable_separately_from_corruption(self):
        """A consumer handles the two differently: an unreadable SCHEMA means
        keep the blob and start fresh; a malformed blob is a defect. Both are
        ``ValueError`` so a broad handler still works."""
        assert issubclass(UnsupportedSnapshotVersion, ValueError)

        snap = _populated().snapshot()
        snap["version"] = "auction-snapshot/v99"
        with pytest.raises(UnsupportedSnapshotVersion):
            Auction.restore(snap)

        corrupt = _populated().snapshot()
        del corrupt["bids"][0]["denom"]
        with pytest.raises(ValueError) as caught:
            Auction.restore(corrupt)
        assert not isinstance(caught.value, UnsupportedSnapshotVersion)

    def test_a_missing_bid_field_raises_and_names_it(self):
        snap = _populated().snapshot()
        del snap["bids"][0]["proofs"]

        with pytest.raises(ValueError, match=r"missing \['proofs'\]"):
            Auction.restore(snap)

    def test_an_unknown_bid_field_raises_and_names_it(self):
        snap = _populated().snapshot()
        snap["bids"][0]["latency_ms"] = 12

        with pytest.raises(ValueError, match=r"unknown \['latency_ms'\]"):
            Auction.restore(snap)

    def test_a_missing_policy_field_raises(self):
        snap = _populated().snapshot()
        del snap["policy"]["required_proofs"]

        with pytest.raises(ValueError, match=r"missing \['required_proofs'\]"):
            Auction.restore(snap)

    def test_a_wrong_top_level_key_set_raises(self):
        snap = _populated().snapshot()
        snap["extra"] = 1

        with pytest.raises(ValueError, match="wrong top-level keys"):
            Auction.restore(snap)

    def test_a_duplicate_bid_key_raises_rather_than_keeping_one(self):
        """Two rows under one key is a blob two auctions were written into.
        ``observe`` would keep one of them and say nothing, because it raises
        only when a key changes PROVIDER."""
        snap = _populated().snapshot()
        snap["bids"].append(copy.deepcopy(snap["bids"][0]))

        with pytest.raises(ValueError, match="duplicate bid_key"):
            Auction.restore(snap)

    def test_an_unknown_preferred_selection_raises(self):
        snap = _populated().snapshot()
        snap["policy"]["preferred_selection"] = "fastest"

        with pytest.raises(ValueError, match="unknown selection"):
            Auction.restore(snap)

    def test_a_bid_that_violates_the_dataclass_is_still_refused_on_restore(self):
        """``BidObservation.__post_init__`` is the same gate on the way back in:
        ``gseq=0`` is the shape a missing value takes when someone defaults an
        absent field to zero."""
        snap = _populated().snapshot()
        snap["bids"][0]["gseq"] = 0

        with pytest.raises(ValueError, match="1-based"):
            Auction.restore(snap)

    def test_a_boolean_gseq_is_refused_rather_than_read_as_group_one(self):
        snap = _populated().snapshot()
        snap["bids"][0]["gseq"] = True

        with pytest.raises(ValueError, match="expected an integer or null"):
            Auction.restore(snap)

    def test_a_non_mapping_snapshot_is_refused(self):
        with pytest.raises(ValueError, match="expected a mapping"):
            Auction.restore([])  # type: ignore[arg-type]

    def test_bids_that_are_not_a_list_are_refused(self):
        snap = _populated().snapshot()
        snap["bids"] = {"akash1lisbon/1/1/0": {}}

        with pytest.raises(ValueError, match="expected a list"):
            Auction.restore(snap)


# ---------------------------------------------------------------------------
# 6. The clock: relative offsets only, and an explicit rebase
# ---------------------------------------------------------------------------
class TestTheClockContract:
    def test_a_snapshot_whose_started_at_is_not_zero_is_refused(self):
        """CPython: *"the reference point of the returned value is undefined."*
        On Linux ``time.monotonic`` is time since BOOT, so a same-host restart
        test passes and a container reschedule produces nonsense. That is worse
        than meaningless, and it is why this refuses instead of accepting."""
        auction = Auction(_policy(), started_at=98765.4)
        auction.observe(_bid("akash1lisbon", "4.2", 98768.9))

        with pytest.raises(ValueError, match="expected 0"):
            Auction.restore(auction.snapshot())

    def test_an_explicit_rebase_accepts_it_and_shifts_every_offset_equally(self):
        auction = Auction(_policy(), started_at=98765.4)
        auction.observe(_bid("akash1lisbon", "4.2", 98768.9))
        auction.observe(_bid("akash1sofia", "3.9", 98775.4))

        restored = Auction.restore(auction.snapshot(), rebase_started_at=0)

        assert restored.started_at == 0
        assert restored._latest_by_key["akash1lisbon/1/1/0"].observed_at == pytest.approx(3.5)
        assert restored._latest_by_key["akash1sofia/1/1/0"].observed_at == pytest.approx(10.0)

    def test_a_rebase_preserves_relative_arrival_order_and_gaps(self):
        auction = Auction(_policy(), started_at=0)
        auction.observe(_bid("akash1lisbon", "4.2", 3.0))
        auction.observe(_bid("akash1sofia", "3.9", 11.0))

        rebased = Auction.restore(auction.snapshot(), rebase_started_at=1000.0)
        gap = (
            rebased._latest_by_key["akash1sofia/1/1/0"].observed_at
            - rebased._latest_by_key["akash1lisbon/1/1/0"].observed_at
        )

        assert rebased.started_at == 1000.0
        assert rebased._latest_by_key["akash1lisbon/1/1/0"].observed_at == pytest.approx(1003.0)
        assert gap == pytest.approx(8.0)

    def test_a_zero_started_snapshot_round_trips_with_the_offsets_untouched(self):
        auction = _populated()

        restored = Auction.restore(auction.snapshot())

        assert restored._latest_by_key["akash1lisbon/1/1/0"].observed_at == 3.5

    def test_a_non_finite_rebase_is_refused(self):
        with pytest.raises(ValueError, match="finite"):
            Auction.restore(_populated().snapshot(), rebase_started_at=float("inf"))


# ---------------------------------------------------------------------------
# 7. Scope — bid_key is unique WITHIN an order, not across the chain
# ---------------------------------------------------------------------------
class TestScopeStopsTwoOrdersMerging:
    """A consumer measured ten live ``bids/list`` rows collapsing to FOUR keys
    across seven deployments: ``provider/gseq/oseq/bseq`` names a different bid
    in every deployment that has one. One ``Auction`` is one order, so its keys
    are unambiguous in memory -- but a snapshot outlives the process, is stored
    beside its siblings, and comes back from a lookup that can be wrong.

    ``observe`` cannot catch the merge: it raises only when a key changes
    PROVIDER, and the collision case is the same provider bidding on two
    deployments. The auction would then select, and the saga would lease, a bid
    placed on somebody else's order.
    """

    def test_a_snapshot_can_carry_the_order_it_belongs_to(self):
        snap = _populated().snapshot(scope="dseq:24680")

        assert snap["scope"] == "dseq:24680"

    def test_restoring_a_scoped_snapshot_into_the_wrong_order_is_refused(self):
        snap = _populated().snapshot(scope="dseq:24680")

        with pytest.raises(ValueError, match="unique within ONE order"):
            Auction.restore(snap, expect_scope="dseq:13579")

    def test_restoring_a_scoped_snapshot_without_saying_which_order_is_refused(self):
        """Fail closed. A caller that wrote the order down and then does not
        check it on the way back in has the identity and is not using it."""
        snap = _populated().snapshot(scope="dseq:24680")

        with pytest.raises(ValueError, match="the caller expected None"):
            Auction.restore(snap)

    def test_expecting_a_scope_from_an_unscoped_snapshot_is_refused(self):
        with pytest.raises(ValueError, match="this snapshot is for None"):
            Auction.restore(_populated().snapshot(), expect_scope="dseq:24680")

    def test_a_matching_scope_restores_and_round_trips_the_scope(self):
        original = _populated()
        snap = original.snapshot(scope="dseq:24680")
        restored = Auction.restore(snap, expect_scope="dseq:24680")

        assert restored.snapshot(scope="dseq:24680") == snap

    def test_scope_is_opaque_to_this_module_and_absent_by_default(self):
        """The core neither parses nor requires it: one Auction is one order,
        and inventing a dseq field here would put a chain concept into a module
        that has none."""
        assert _populated().snapshot()["scope"] is None

    def test_an_empty_scope_is_refused_because_it_is_not_an_identity(self):
        with pytest.raises(ValueError, match="non-empty string or None"):
            _populated().snapshot(scope="")


# ---------------------------------------------------------------------------
# 8. The behavioural test — a resumed auction decides identically
# ---------------------------------------------------------------------------
class TestResumeDecidesIdentically:
    """Round-trip equality of the STATE is necessary and not sufficient. What a
    consumer actually needs is that the VERDICT is unchanged, so these compare
    whole ``AuctionResult`` values -- ``selected``, ``selection_reason``,
    ``considered``, ``rejected`` and the deadlines all at once.
    """

    def test_snapshot_restore_evaluate_equals_an_uninterrupted_evaluate(self):
        uninterrupted = _populated()
        resumed = Auction.restore(uninterrupted.snapshot())

        for now in (0.0, 20.0, 45.0, 60.0, 135.0, 200.0):
            assert resumed.evaluate(now=now) == uninterrupted.evaluate(now=now), now

    def test_the_verdict_survives_a_crash_between_two_polls(self):
        """The real shape of a crash: some bids collected, process dies, the
        rest arrive after the restart. The winner must be what it would have
        been had nothing happened."""
        policy = _policy(preferred_providers=frozenset({"akash1lisbon"}))

        uninterrupted = Auction(policy, started_at=0)
        uninterrupted.observe(_bid("akash1backup", "1.0", 2.0))
        uninterrupted.observe(_bid("akash1lisbon", "9.0", 30.0))

        crashed = Auction(policy, started_at=0)
        crashed.observe(_bid("akash1backup", "1.0", 2.0))
        resumed = Auction.restore(crashed.snapshot())
        resumed.observe(_bid("akash1lisbon", "9.0", 30.0))

        assert resumed.evaluate(now=45) == uninterrupted.evaluate(now=45)
        assert resumed.evaluate(now=45).selected.provider == "akash1lisbon"
        assert resumed.evaluate(now=45).selection_reason == "cheapest_preferred"

    def test_first_arrival_survives_the_restart_and_is_still_immutable(self):
        """The fallback rule orders by first arrival. A restart that re-dated
        the surviving bids would hand it a pool that all arrived at once, and
        "first observed" would degenerate into a tie-break on the key."""
        policy = _policy(preferred_providers=frozenset())

        auction = Auction(policy, started_at=0)
        auction.observe(_bid("akash1backup", "9.0", 2.0))
        auction.observe(_bid("akash1sofia", "1.0", 4.0))

        resumed = Auction.restore(auction.snapshot())
        # The same two bids seen again on the poll after the restart, cheaper.
        resumed.observe(_bid("akash1backup", "8.0", 46.0))
        resumed.observe(_bid("akash1sofia", "0.5", 46.0))

        decision = resumed.evaluate(now=46)

        assert decision.status is AuctionStatus.DECIDED
        assert decision.selection_reason == "first_eligible_fallback"
        assert decision.selected.provider == "akash1backup"
        assert decision.selected.observed_at == 2.0
        assert decision.selected.price == Decimal("8.0")

    def test_an_expired_auction_resumes_as_expired_with_the_same_rejections(self):
        auction = Auction(_policy(), started_at=0)
        auction.observe(_bid("akash1blocked", "0.1", 1.0))
        auction.observe(_bid("akash1stranger", "0.2", 2.0))

        resumed = Auction.restore(auction.snapshot())

        assert resumed.evaluate(now=200) == auction.evaluate(now=200)
        assert resumed.evaluate(now=200).status is AuctionStatus.EXPIRED

    def test_the_emptiest_verdict_survives_a_restart(self):
        """Capacity is the field a lossy serializer is most likely to flatten,
        and ``EMPTIEST`` is the mode that reads it."""
        policy = _policy(
            preferred_selection=PreferredSelection.EMPTIEST,
            required_proofs=frozenset(),
        )
        auction = Auction(policy, started_at=0)
        auction.observe(
            _bid("akash1lisbon", "9.0", 1.0, capacity=ProviderCapacity(cpu=0.8, gpu=0.7))
        )
        auction.observe(
            _bid("akash1sofia", "1.0", 2.0, capacity=ProviderCapacity(cpu=0.2, gpu=0.1))
        )

        resumed = Auction.restore(auction.snapshot())
        decision = resumed.evaluate(now=45)

        assert decision == auction.evaluate(now=45)
        assert decision.selection_reason == "emptiest_preferred"
        assert decision.selected.provider == "akash1lisbon"

    def test_a_mixed_denomination_pool_still_fails_closed_after_a_restart(self):
        auction = Auction(_policy(preferred_providers=frozenset()), started_at=0)
        auction.observe(_bid("akash1lisbon", "4.2", 1.0, denom="uakt"))
        auction.observe(_bid("akash1sofia", "3.9", 2.0, denom="uact"))

        resumed = Auction.restore(auction.snapshot())

        with pytest.raises(ValueError, match="different denominations"):
            resumed.evaluate(now=45)

    def test_a_rebased_resume_decides_the_same_thing_on_a_shifted_clock(self):
        policy = _policy(preferred_providers=frozenset())
        auction = Auction(policy, started_at=0)
        auction.observe(_bid("akash1backup", "9.0", 2.0))
        auction.observe(_bid("akash1sofia", "1.0", 4.0))

        rebased = Auction.restore(auction.snapshot(), rebase_started_at=5_000.0)
        shifted = rebased.evaluate(now=5_045.0)
        original = auction.evaluate(now=45.0)

        # Not `==` on the whole result: the timestamps ARE different, and that is
        # the point of a rebase. What must not move is the verdict.
        assert shifted.status is original.status
        assert shifted.selection_reason == original.selection_reason
        assert shifted.selected.bid_key == original.selected.bid_key
        assert shifted.selected.price == original.selected.price
        assert [item.bid_key for item in shifted.considered] == [
            item.bid_key for item in original.considered
        ]
