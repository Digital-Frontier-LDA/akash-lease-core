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


def test_without_preferred_bid_selects_cheapest_eligible_fallback():
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
    assert result.selected.provider == "hurricane"
    assert result.selection_reason == "cheapest_eligible_fallback"


def test_none_eligible_population_means_any_provider_may_fallback():
    auction = Auction(
        AuctionPolicy(collection_window_seconds=10, preferred_providers=frozenset({"lisbon"})),
        started_at=0,
    )
    auction.observe(bid("unknown-a", "8", 1))
    auction.observe(bid("unknown-b", "2", 2))

    result = auction.evaluate(now=10)

    assert result.selected is not None
    assert result.selected.provider == "unknown-b"


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


def test_equal_prices_have_a_stable_provider_then_bid_key_tiebreak():
    policy = AuctionPolicy(collection_window_seconds=10)
    first = Auction(policy, started_at=0)
    second = Auction(policy, started_at=0)
    observations = [bid("z-provider", "5", 1, bid_key="z"), bid("a-provider", "5", 9, bid_key="a")]
    for observation in observations:
        first.observe(observation)
    for observation in reversed(observations):
        second.observe(observation)

    assert first.evaluate(now=10).selected == second.evaluate(now=10).selected
    assert first.evaluate(now=10).selected.provider == "a-provider"


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

    result = auction.evaluate(now=10)

    assert result.status is AuctionStatus.EXPIRED
    assert result.selected is None
    assert result.selection_reason == "no_eligible_open_bids"
    assert len(result.rejected) == 1
