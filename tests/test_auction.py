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
