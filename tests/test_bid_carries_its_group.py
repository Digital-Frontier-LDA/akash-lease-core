"""A selected bid must name the group it belongs to.

just-akash#195, measured: `_cheapest_bid` picks one bid across ALL groups and
the leasing path then leases `gseq=1` regardless (`api.py:413`, hardcoded). A
winning bid for group 7 is leased as group 1 and the lease FAILS.

⭐ The gain that unlocks is large and measured: splitting an order into groups
roughly DOUBLES the bid rate — 143/191 (74.9%) for one group vs 111/303 (36.6%)
for twelve — because a provider that can satisfy some of twelve resources cannot
bid at all when they are one indivisible group. That is unreachable while the
winner cannot name its own group.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from akash_lease_core.auction import (
    Auction,
    AuctionPolicy,
    BidObservation,
)


def _bid(key: str, provider: str, price: str, at: float, gseq: int | None = None):
    return BidObservation(
        bid_key=key,
        provider=provider,
        price=Decimal(price),
        denom="uakt",
        observed_at=at,
        gseq=gseq,
    )


def test_a_selected_bid_carries_the_group_it_was_made_for() -> None:
    """⭐ THE POINT. Without this the caller has only `.provider`, and the group
    must be recovered by parsing `bid_key` — or, as the consumer actually does,
    assumed to be 1."""
    policy = AuctionPolicy(collection_window_seconds=10, preferred_providers={"p1", "p2"})
    a = Auction(policy, started_at=0.0)
    a.observe(_bid("p1/7/1", "p1", "3", 1.0, gseq=7))
    a.observe(_bid("p2/4/1", "p2", "9", 2.0, gseq=4))
    result = a.evaluate(now=11.0)
    assert result.selected.provider == "p1"
    assert result.selected.gseq == 7, "the winner must name ITS OWN group, not group 1"


def test_not_supplied_stays_None_and_is_NEVER_silently_one() -> None:
    """⛔ Defaulting an absent group to 1 reproduces the exact defect: it makes a
    bid for group 7 indistinguishable from a bid for group 1."""
    assert _bid("k", "p", "1", 1.0).gseq is None


@pytest.mark.parametrize("bad", [0, -1, -7])
def test_a_zero_or_negative_group_is_rejected(bad: int) -> None:
    """Akash groups are 1-based. A 0 is the shape an absent value takes when
    someone defaults a missing field to zero rather than to None."""
    with pytest.raises(ValueError, match="1-based"):
        _bid("k", "p", "1", 1.0, gseq=bad)


def test_a_bool_is_rejected_because_True_would_mean_group_one() -> None:
    """⚠ `bool` IS `int` in Python, so `gseq=True` would pass an int check and
    silently mean group 1 — the defect this field exists to prevent, arriving
    through the type system."""
    with pytest.raises(ValueError, match="int or None"):
        _bid("k", "p", "1", 1.0, gseq=True)


def test_KNOWN_NEGATIVE_the_group_is_not_recoverable_from_provider_alone() -> None:
    """⭐ The control that shows the field adds something. Two bids from the SAME
    provider for DIFFERENT groups are indistinguishable by `.provider`; only
    `.gseq` separates them."""
    a = _bid("p1/3/1", "p1", "5", 1.0, gseq=3)
    b = _bid("p1/9/1", "p1", "5", 2.0, gseq=9)
    assert a.provider == b.provider, "control: same provider"
    assert a.gseq != b.gseq, "and only gseq distinguishes the groups"


def test_the_existing_contract_is_unchanged_when_gseq_is_omitted() -> None:
    """Additive: every existing caller keeps working, and selection is untouched."""
    policy = AuctionPolicy(collection_window_seconds=10, preferred_providers={"p1", "p2"})
    a = Auction(policy, started_at=0.0)
    a.observe(_bid("k1", "p1", "9", 1.0))
    a.observe(_bid("k2", "p2", "1", 2.0))
    r = a.evaluate(now=11.0)
    assert r.selected.provider == "p2"
    assert r.selection_reason == "cheapest_preferred"
    assert r.selected.gseq is None
