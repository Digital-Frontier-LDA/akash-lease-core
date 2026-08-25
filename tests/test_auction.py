from decimal import Decimal

import pytest

from akash_lease_core.auction import (
    Auction,
    AuctionPolicy,
    AuctionStatus,
    BidObservation,
    MixedBidDenominations,
)


def test_auction_contract_is_exported_from_the_package_root():
    from akash_lease_core import Auction as RootAuction
    from akash_lease_core import AuctionPolicy as RootAuctionPolicy

    assert RootAuction is Auction
    assert RootAuctionPolicy is AuctionPolicy


def bid(
    provider: str,
    price: str,
    observed_at: float,
    *,
    state: str = "open",
    denom: str = "uact",
    bid_key: str | None = None,
) -> BidObservation:
    return BidObservation(
        bid_key=bid_key or provider,
        provider=provider,
        price=Decimal(price),
        denom=denom,
        observed_at=observed_at,
        state=state,
    )


def test_collects_for_the_entire_window_even_when_a_preferred_bid_arrives_early():
    auction = Auction(
        AuctionPolicy(collection_window_seconds=60, preferred_providers=frozenset({"lisbon"})),
        started_at=100,
    )
    auction.observe(bid("lisbon", "12", 101))

    result = auction.evaluate(now=159.999)

    assert result.status is AuctionStatus.COLLECTING
    assert result.selected is None


def test_late_arriving_preferred_beats_early_backup_inside_grace_window():
    """60s grace contract.

    A backup bid observed early in the window MUST NOT win against a preferred bid
    observed later but still inside the 60-second collection window. The library
    enforces equal-opportunity by accumulating observations and choosing at the
    deadline; an adapter that returns on the first non-empty poll would instead
    pick the backup. This test pins down the contract any adapter must honor.

    See C5 structural review, Deep dive 5 ("provider selection and qualification
    control planes"), and the akash-lease-core C5 tracking issue.
    """
    auction = Auction(
        AuctionPolicy(
            collection_window_seconds=60,
            preferred_providers=frozenset({"lisbon"}),
            eligible_providers=frozenset({"lisbon", "hurricane"}),
        ),
        started_at=0,
    )

    # Backup bid arrives at t=3 (well inside the window).
    auction.observe(bid("hurricane", "1", 3))
    # While still collecting, an evaluate() call sees the backup but stays COLLECTING.
    mid_window = auction.evaluate(now=10)
    assert mid_window.status is AuctionStatus.COLLECTING
    assert mid_window.selected is None
    assert mid_window.selection_reason == "collection_window_open"

    # Preferred bid arrives late in the window at t=9 (still inside the 60s grace).
    auction.observe(bid("lisbon", "5", 9))

    # At the deadline the cheapest preferred (lisbon) wins, not the cheaper backup.
    decided = auction.evaluate(now=60)
    assert decided.status is AuctionStatus.DECIDED
    assert decided.selected is not None
    assert decided.selected.provider == "lisbon"
    assert decided.selection_reason == "cheapest_preferred"


def test_preferred_observed_one_second_before_deadline_still_beats_early_backup():
    """Boundary case: a preferred bid arriving one tick before the deadline wins.

    Reinforces the equal-opportunity contract: the deadline is the exclusive moment
    of choice, not a soft early-exit. A consumer that times out slightly before 60s
    would lose this preferred bid; this test makes that misimplementation visible.
    """
    auction = Auction(
        AuctionPolicy(
            collection_window_seconds=60,
            preferred_providers=frozenset({"lisbon"}),
            eligible_providers=frozenset({"lisbon", "hurricane"}),
        ),
        started_at=0,
    )
    auction.observe(bid("hurricane", "1", 1))
    auction.observe(bid("lisbon", "9", 59.999))

    result = auction.evaluate(now=60)

    assert result.status is AuctionStatus.DECIDED
    assert result.selected is not None
    assert result.selected.provider == "lisbon"
    assert result.selection_reason == "cheapest_preferred"


def test_at_deadline_selects_cheapest_preferred_not_first_preferred():
    auction = Auction(
        AuctionPolicy(
            collection_window_seconds=60,
            preferred_providers=frozenset({"sofia", "lisbon"}),
            eligible_providers=frozenset({"sofia", "lisbon", "hurricane"}),
        ),
        started_at=0,
    )
    auction.observe(bid("sofia", "20", 1))
    auction.observe(bid("hurricane", "1", 2))
    auction.observe(bid("lisbon", "10", 59))

    result = auction.evaluate(now=60)

    assert result.status is AuctionStatus.DECIDED
    assert result.selected is not None
    assert result.selected.provider == "lisbon"
    assert result.selection_reason == "cheapest_preferred"


def test_without_preferred_bid_selects_first_observed_eligible_fallback():
    auction = Auction(
        AuctionPolicy(
            collection_window_seconds=30,
            preferred_providers=frozenset({"lisbon"}),
            eligible_providers=frozenset({"helsinki", "hurricane"}),
        ),
        started_at=10,
    )
    auction.observe(bid("helsinki", "7", 11))
    auction.observe(bid("hurricane", "3", 39))

    result = auction.evaluate(now=40)

    assert result.selected is not None
    assert result.selected.provider == "helsinki"
    assert result.selection_reason == "first_eligible_fallback"


def test_none_eligible_population_means_any_provider_may_fallback():
    auction = Auction(
        AuctionPolicy(collection_window_seconds=10, preferred_providers=frozenset({"lisbon"})),
        started_at=0,
    )
    auction.observe(bid("unknown-a", "8", 1))
    auction.observe(bid("unknown-b", "2", 2))

    result = auction.evaluate(now=70)

    assert result.selected is not None
    assert result.selected.provider == "unknown-a"


def test_after_preferred_window_waits_for_first_eligible_fallback():
    auction = Auction(
        AuctionPolicy(
            collection_window_seconds=60,
            fallback_window_seconds=30,
            preferred_providers=frozenset({"lisbon", "sofia", "helsinki"}),
            eligible_providers=frozenset({"lisbon", "sofia", "helsinki", "hurricane"}),
        ),
        started_at=0,
    )

    waiting = auction.evaluate(now=60)
    assert waiting.status is AuctionStatus.COLLECTING
    assert waiting.selection_reason == "waiting_for_first_eligible_fallback"

    auction.observe(bid("hurricane", "99", 63))
    decided = auction.evaluate(now=63)
    assert decided.status is AuctionStatus.DECIDED
    assert decided.selected is not None
    assert decided.selected.provider == "hurricane"


def test_fallback_phase_is_bounded_when_nobody_bids():
    auction = Auction(
        AuctionPolicy(collection_window_seconds=60, fallback_window_seconds=30),
        started_at=10,
    )
    assert auction.evaluate(now=99.9).status is AuctionStatus.COLLECTING
    assert auction.evaluate(now=100).status is AuctionStatus.EXPIRED


def test_preferred_bid_after_preferred_deadline_cannot_displace_fallback():
    auction = Auction(
        AuctionPolicy(
            collection_window_seconds=60,
            fallback_window_seconds=30,
            preferred_providers=frozenset({"lisbon"}),
        ),
        started_at=0,
    )
    auction.observe(bid("hurricane", "9", 61))
    auction.observe(bid("lisbon", "1", 62))
    result = auction.evaluate(now=62)
    assert result.selected is not None
    assert result.selected.provider == "hurricane"
    assert result.selection_reason == "first_eligible_fallback"


def test_bid_observed_after_fallback_deadline_cannot_revive_expired_auction():
    auction = Auction(
        AuctionPolicy(collection_window_seconds=60, fallback_window_seconds=30),
        started_at=0,
    )
    auction.observe(bid("hurricane", "1", 91))
    result = auction.evaluate(now=91)
    assert result.status is AuctionStatus.EXPIRED
    assert result.selected is None


def test_explicit_eligible_population_rejects_foreign_bids():
    auction = Auction(
        AuctionPolicy(
            collection_window_seconds=10,
            preferred_providers=frozenset({"lisbon"}),
            eligible_providers=frozenset({"lisbon", "sofia"}),
        ),
        started_at=0,
    )
    auction.observe(bid("foreign", "1", 1))
    auction.observe(bid("sofia", "9", 2))

    result = auction.evaluate(now=10)

    assert result.selected is not None
    assert result.selected.provider == "sofia"
    assert result.rejected[0].provider == "foreign"
    assert result.rejected[0].reason == "provider_not_eligible"


def test_exclusion_overrides_preference_and_eligibility():
    auction = Auction(
        AuctionPolicy(
            collection_window_seconds=10,
            preferred_providers=frozenset({"lisbon"}),
            eligible_providers=frozenset({"lisbon", "sofia"}),
            excluded_providers=frozenset({"lisbon"}),
        ),
        started_at=0,
    )
    auction.observe(bid("lisbon", "1", 1))
    auction.observe(bid("sofia", "5", 2))

    result = auction.evaluate(now=10)

    assert result.selected is not None
    assert result.selected.provider == "sofia"
    assert any(
        item.provider == "lisbon" and item.reason == "provider_excluded"
        for item in result.rejected
    )


def test_latest_state_for_a_bid_key_prevents_selecting_a_closed_bid():
    auction = Auction(
        AuctionPolicy(collection_window_seconds=10, preferred_providers=frozenset({"lisbon"})),
        started_at=0,
    )
    auction.observe(bid("lisbon", "1", 1, bid_key="bid-1"))
    auction.observe(bid("lisbon", "1", 9, bid_key="bid-1", state="closed"))
    auction.observe(bid("sofia", "4", 2, bid_key="bid-2"))

    result = auction.evaluate(now=10)

    assert result.selected is not None
    assert result.selected.provider == "sofia"
    assert any(
        item.bid_key == "bid-1" and item.reason == "bid_not_open" for item in result.rejected
    )


def test_fallback_selection_is_stable_by_observation_then_provider_bid_key():
    policy = AuctionPolicy(collection_window_seconds=10)
    first = Auction(policy, started_at=0)
    second = Auction(policy, started_at=0)
    observations = [bid("z-provider", "5", 1, bid_key="z"), bid("a-provider", "5", 9, bid_key="a")]
    for observation in observations:
        first.observe(observation)
    for observation in reversed(observations):
        second.observe(observation)

    assert first.evaluate(now=10).selected == second.evaluate(now=10).selected
    assert first.evaluate(now=10).selected.provider == "z-provider"


def test_mixed_denominations_fail_closed_instead_of_comparing_unlike_prices():
    auction = Auction(AuctionPolicy(collection_window_seconds=10), started_at=0)
    auction.observe(bid("akt-provider", "1", 1, denom="uakt"))
    auction.observe(bid("act-provider", "2", 2, denom="uact"))

    with pytest.raises(MixedBidDenominations, match="uact.*uakt|uakt.*uact"):
        auction.evaluate(now=10)


@pytest.mark.parametrize("seconds", [-1, 60.001])
def test_collection_window_must_be_non_negative_and_no_more_than_sixty_seconds(seconds):
    with pytest.raises(ValueError, match="collection_window_seconds"):
        AuctionPolicy(collection_window_seconds=seconds)


def test_zero_second_window_supports_immediate_deterministic_selection():
    auction = Auction(AuctionPolicy(collection_window_seconds=0), started_at=10)
    auction.observe(bid("provider", "2", 10))

    result = auction.evaluate(now=10)

    assert result.status is AuctionStatus.DECIDED
    assert result.selected is not None
    assert result.selected.provider == "provider"


def test_no_eligible_open_bid_expires_with_rejection_evidence():
    auction = Auction(
        AuctionPolicy(
            collection_window_seconds=10,
            eligible_providers=frozenset({"lisbon"}),
        ),
        started_at=0,
    )
    auction.observe(bid("foreign", "1", 1))

    result = auction.evaluate(now=70)

    assert result.status is AuctionStatus.EXPIRED
    assert result.selected is None
    assert result.selection_reason == "no_eligible_open_bids"
    assert len(result.rejected) == 1


# ---------------------------------------------------------------------------
# Typed-evidence machinery (C5 #13 item 3)
# ---------------------------------------------------------------------------
#
# The library exposes a typed-evidence concept so consumers can declare which
# proof IDs a bid must carry before they will accept the decision. The library
# does NOT reject bids for missing proofs — it surfaces the gap on the result
# and lets the consumer decide. This keeps selection logic uniform (item 1)
# while letting consumers like the tier-roster's `requires_proofs` machinery
# gate on evidence.


def _bid_with_proofs(
    provider: str,
    price: str,
    observed_at: float,
    proofs: tuple[str, ...],
    *,
    bid_key: str | None = None,
) -> BidObservation:
    return BidObservation(
        bid_key=bid_key or provider,
        provider=provider,
        price=Decimal(price),
        denom="uact",
        observed_at=observed_at,
        proofs=proofs,
    )


def test_no_required_proofs_means_no_missing_proofs_reported():
    """Default ``required_proofs`` is empty -> every decision is complete.

    Consumers that do not declare evidence requirements see the same
    selection behavior as before this field existed; the regression-guard
    is the equality check.
    """
    auction = Auction(
        AuctionPolicy(
            collection_window_seconds=10,
            preferred_providers=frozenset({"lisbon"}),
        ),
        started_at=0,
    )
    auction.observe(bid("lisbon", "1", 1))

    result = auction.evaluate(now=10)

    assert result.selected is not None
    assert result.selected.provider == "lisbon"
    assert result.missing_required_proofs == ()


def test_selected_winner_surfaces_proofs_required_by_policy_but_not_carried():
    """A bid without required proofs still wins selection; the gap is surfaced.

    Selection is by price/order (uniform contract). The consumer decides
    whether to accept a decision whose selected winner lacks a required
    proof — typical patterns are: re-evaluate with stricter acceptance,
    retry, or block.
    """
    auction = Auction(
        AuctionPolicy(
            collection_window_seconds=10,
            preferred_providers=frozenset({"lisbon"}),
            required_proofs=frozenset({"restart_observed", "provider_quote_hash"}),
        ),
        started_at=0,
    )
    auction.observe(_bid_with_proofs("lisbon", "1", 1, proofs=("restart_observed",)))

    result = auction.evaluate(now=10)

    assert result.selected is not None
    assert result.selected.provider == "lisbon"
    assert result.missing_required_proofs == ("provider_quote_hash",)


def test_selected_winner_with_all_required_proofs_reports_no_gaps():
    auction = Auction(
        AuctionPolicy(
            collection_window_seconds=10,
            preferred_providers=frozenset({"lisbon"}),
            required_proofs=frozenset({"restart_observed", "provider_quote_hash"}),
        ),
        started_at=0,
    )
    auction.observe(
        _bid_with_proofs(
            "lisbon",
            "1",
            1,
            proofs=("restart_observed", "provider_quote_hash"),
        )
    )

    result = auction.evaluate(now=10)

    assert result.selected is not None
    assert result.missing_required_proofs == ()


def test_required_proofs_do_not_reject_bid_from_selection():
    """Selection logic stays uniform: only price/order and policy membership filter.

    A bid missing proofs is still considered for selection; the missing
    proofs are surfaced on the result, not used to silently filter the
    candidate pool. This keeps item 1 (uniformity) honest while exposing
    item 3 (typed-evidence) information.
    """
    auction = Auction(
        AuctionPolicy(
            collection_window_seconds=10,
            preferred_providers=frozenset({"lisbon"}),
            required_proofs=frozenset({"provider_quote_hash"}),
        ),
        started_at=0,
    )
    # lisbon is preferred but has no proofs; helsinki is fallback and has proofs.
    auction.observe(_bid_with_proofs("lisbon", "1", 1, proofs=()))
    auction.observe(_bid_with_proofs("helsinki", "9", 2, proofs=("provider_quote_hash",)))

    result = auction.evaluate(now=10)

    # Cheapest preferred still wins even with missing proofs — selection is uniform.
    assert result.selected is not None
    assert result.selected.provider == "lisbon"
    # Consumer learns the gap on the result, not via selection rejection.
    assert result.missing_required_proofs == ("provider_quote_hash",)


def test_missing_proofs_are_reported_sorted_for_deterministic_consumer_comparison():
    """Stable order so consumers can hash the result for caching/audit."""
    auction = Auction(
        AuctionPolicy(
            collection_window_seconds=10,
            preferred_providers=frozenset({"lisbon"}),
            required_proofs=frozenset({"a", "b", "c"}),
        ),
        started_at=0,
    )
    # lisbon carries "c" only -> the missing proofs are "a" and "b", reported sorted.
    auction.observe(_bid_with_proofs("lisbon", "1", 1, proofs=("c",)))

    result = auction.evaluate(now=10)

    assert result.missing_required_proofs == ("a", "b")


def test_fallback_path_also_reports_missing_proofs_on_selected_winner():
    auction = Auction(
        AuctionPolicy(
            collection_window_seconds=10,
            fallback_window_seconds=5,
            preferred_providers=frozenset({"lisbon"}),
            eligible_providers=frozenset({"lisbon", "hurricane"}),
            required_proofs=frozenset({"lease_liveness_proof"}),
        ),
        started_at=0,
    )
    # No lisbon bid; hurricane is fallback and carries no proof.
    auction.observe(_bid_with_proofs("hurricane", "5", 12, proofs=()))

    result = auction.evaluate(now=13)

    assert result.selected is not None
    assert result.selected.provider == "hurricane"
    assert result.missing_required_proofs == ("lease_liveness_proof",)


# ─── first-seen contract: re-observation must not erase the first sighting ───
#
# ⛔ THE DEFECT THIS REGRESSION GUARDS. `Auction.observe` used to replace a stored
# bid whenever `observed_at >= current.observed_at`, so a real adapter that polls
# every 3 s would rewrite every bid's recorded arrival to its LATEST sighting.
# With every observed bid carrying the last poll's timestamp, "first observed"
# degenerated into a tie-break on provider/bid_key and the FALLBACK RULE chose by
# the last index, not by arrival. Every observe-once unit test still passed
# because the bug only appears when the same bid is observed twice.
#
# `console_api_backend.poll_bids` already guards this on the Console path (see
# `first_seen` in control-plane/api/services/console_api_backend.py:319-354 of
# Borduas-Holdings/Blazing-Back). PR #1586's `BidAuction.observe_raw` carries the
# same guard on the raw-polling path. The fix below lifts the guard into the
# core so every consumer inherits it — and this test pins it.


def test_observe_preserves_first_sighting_when_same_bid_re_observed():
    """The same `bid_key` observed twice must keep its FIRST arrival time.

    Setup: a real Console poll loop stamps every bid with the monotonic time of
    the poll that saw it. A later poll that re-sees the same bid carries a
    strictly-later timestamp. If the core overwrites on `>=`, the FIRST arrival
    is silently rewritten to the latest poll — every bid still in the candidate
    pool shares the last poll's timestamp, "first observed" becomes a tie-break,
    and the FALLBACK RULE breaks under real polling while every observe-once
    unit test still passes.
    """
    auction = Auction(AuctionPolicy(), started_at=0)
    first_seen_at = 10.0
    later_re_observed_at = 13.0  # strictly later, as a real second poll stamps

    auction.observe(
        BidObservation(
            bid_key="akash1provider/1/1",
            provider="akash1provider",
            price=Decimal("5"),
            denom="uakt",
            observed_at=first_seen_at,
            state="open",
        )
    )
    auction.observe(
        BidObservation(
            bid_key="akash1provider/1/1",
            provider="akash1provider",
            price=Decimal("5"),
            denom="uakt",
            observed_at=later_re_observed_at,
            state="open",
        )
    )

    stored = auction._latest_by_key["akash1provider/1/1"]
    assert stored.observed_at == first_seen_at, (
        f"observe() rewrote the first sighting's arrival: "
        f"got {stored.observed_at}, expected {first_seen_at}. "
        f"Re-observation must keep first-arrival and refresh only mutable state."
    )


def test_fallback_selection_uses_first_arrival_when_bid_re_observed():
    """The FALLBACK RULE must read by first sighting, not by the last poll's timestamp.

    ⛔ END-TO-END SHAPE OF THE BUG. The primary test pins the storage invariant;
    this test pins the SELECTION invariant. Two providers bid in different polls:

      poll 1 (t=10): helsinki observed
      poll 2 (t=13): hurricane observed  (cheaper, but LATER)
      poll 3 (t=15): helsinki RE-observed (the same `bid_key`, later timestamp)

    With the broken `observe()`, helsinki carries observed_at=15 and hurricane
    carries observed_at=13, so the fallback rule picks hurricane — the CHEAPER
    but LATER provider. With the fix, helsinki keeps observed_at=10 (first
    sighting) and is selected, even though hurricane is cheaper, because the
    fallback rule is "first eligible", not "cheapest eligible".
    """
    auction = Auction(
        AuctionPolicy(
            collection_window_seconds=30,
            preferred_providers=frozenset({"lisbon"}),
            eligible_providers=frozenset({"helsinki", "hurricane"}),
        ),
        started_at=0,
    )
    # Poll 1: helsinki first sighting.
    auction.observe(
        BidObservation(
            bid_key="helsinki/1/1",
            provider="helsinki",
            price=Decimal("10"),
            denom="uakt",
            observed_at=10.0,
            state="open",
        )
    )
    # Poll 2: hurricane appears (cheaper, but LATER — and no preferred provider is open).
    auction.observe(
        BidObservation(
            bid_key="hurricane/1/1",
            provider="hurricane",
            price=Decimal("3"),
            denom="uakt",
            observed_at=13.0,
            state="open",
        )
    )
    # Poll 3: helsinki re-observed with the LAST poll's timestamp. The broken
    # `observe()` overwrites helsinki.observed_at from 10.0 to 15.0 here.
    auction.observe(
        BidObservation(
            bid_key="helsinki/1/1",
            provider="helsinki",
            price=Decimal("10"),
            denom="uakt",
            observed_at=15.0,
            state="open",
        )
    )

    result = auction.evaluate(now=30)
    assert result.status is AuctionStatus.DECIDED
    assert result.selected is not None
    assert result.selected.provider == "helsinki", (
        f"fallback rule picked {result.selected.provider} at observed_at "
        f"{result.selected.observed_at}, but the FIRST eligible fallback by arrival "
        f"order is helsinki (first seen at 10.0). The rule is 'first eligible', "
        f"not 'cheapest eligible' — see `_DEFAULT_FAILOVER_PRIORITY` commentary in "
        f"Blazing-Back's bid_auction.py."
    )
    assert result.selected.observed_at == 10.0
    assert result.selection_reason == "first_eligible_fallback"
