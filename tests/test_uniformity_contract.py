"""Regression-guard for the uniform provider-auction contract (C5 #13 item 1).

C5 structural review, item 1: confirm that ``Auction`` / ``AuctionPolicy``
produce identical selection against identical input across the three
downstream consumers:

1. Console adapter path via Blazing-Back
   (``control-plane/api/services/console_api_backend.py`` ->
   ``deployment_router._select_vetted_bid``)
2. Wallet path via Blazing-Back
   (``control-plane/api/services/akash_client.py``)
3. Compiler GPU burst path
   (``compiler/core/akash_bid_fetcher.py`` +
   ``compiler/core/gpu_bid_planner.py``)

Audit results (2026-08-23):

* Wallet path (``akash_client.py:3097-3138``) calls into this library
  directly and uses the same ``AuctionPolicy`` shape as every other
  consumer.
* Console adapter (``deployment_router.py:340-380``, called from
  ``console_api_backend.py``) calls into this library directly.
* Compiler GPU burst is **divergent**: ``akash_bid_fetcher.py``
  collects its own bid list and ``gpu_bid_planner.py:320`` sorts by an
  efficiency metric (``tok/s/$``), bypassing this library entirely.

The library itself is uniform. A change here cannot fix the consumer-side
divergence, but it CAN pin down the contract so any drift in the library
that would *worsen* the divergence (e.g. a non-deterministic selection
rule, or a default that silently changes ordering) is caught by these
tests.

These tests are the regression-guard half of item 1 of #13. The
divergence itself is captured in the consumer-side audit and is out of
scope for this PR; it is being fixed in DigitalFrontier-infra.
"""

from __future__ import annotations

from decimal import Decimal

from akash_lease_core.auction import (
    Auction,
    AuctionPolicy,
    AuctionResult,
    AuctionStatus,
    BidObservation,
    MixedBidDenominations,
)


def _bid(
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


def _replay(
    policy: AuctionPolicy,
    observations: list[BidObservation],
    *,
    now: float,
) -> AuctionResult:
    auction = Auction(policy, started_at=0)
    for observation in observations:
        auction.observe(observation)
    return auction.evaluate(now=now)


# ---------------------------------------------------------------------------
# Conformance fixtures — every downstream consumer must reproduce this verdict
# ---------------------------------------------------------------------------

# Scenario from the C5 review (Deep dive 5): a backup bid arrives at t=3, a
# preferred bid arrives at t=9, and the consumer evaluates at the deadline.
# The Console adapter's pre-rewrite first-non-empty poll would pick hurricane;
# the post-rewrite 60s grace window must pick lisbon. The wallet path already
# honors this via the library. The compiler GPU burst path uses its own
# efficiency sort and currently picks hurricane on the same input — the
# non-uniformity that this regression-guard exists to make visible.
_GRACE_TIMELINE = [
    _bid("hurricane", "1", 3),
    _bid("lisbon", "5", 9),
]
_GRACE_POLICY = AuctionPolicy(
    collection_window_seconds=60,
    preferred_providers=frozenset({"lisbon"}),
    eligible_providers=frozenset({"lisbon", "hurricane"}),
)


def test_conformance_grace_timeline_selects_preferred_over_early_backup():
    result = _replay(_GRACE_POLICY, _GRACE_TIMELINE, now=60)

    assert result.status is AuctionStatus.DECIDED
    assert result.selected is not None
    assert result.selected.provider == "lisbon"
    assert result.selection_reason == "cheapest_preferred"


def test_conformance_grace_timeline_is_independent_of_observation_order():
    forward = _replay(_GRACE_POLICY, list(_GRACE_TIMELINE), now=60)
    reverse = _replay(_GRACE_POLICY, list(reversed(_GRACE_TIMELINE)), now=60)

    assert forward.selected == reverse.selected
    assert forward.selected.provider == "lisbon"


def test_conformance_grace_timeline_rejects_backup_before_deadline():
    """Mid-window evaluate must NOT short-circuit to hurricane.

    A consumer that returns on the first non-empty poll would have already
    emitted hurricane before lisbon's late bid arrived. Replaying the
    timeline up to t=10 must report COLLECTING, not DECIDED.
    """
    mid_window = _replay(_GRACE_POLICY, _GRACE_TIMELINE, now=10)

    assert mid_window.status is AuctionStatus.COLLECTING
    assert mid_window.selected is None
    assert mid_window.selection_reason == "collection_window_open"


# ---------------------------------------------------------------------------
# Determinism across permutations — same bid set, different adapter order
# ---------------------------------------------------------------------------


def test_preferred_selection_is_independent_of_observation_order():
    """Symmetric counterpart to ``test_fallback_selection_is_stable_*``.

    Three preferred bids observed in any order must yield the same winner
    (cheapest preferred) at the deadline. Any consumer that exposes a
    different ordering rule (e.g. first-observed, failover_priority sorted)
    must be re-aligned to this verdict.
    """
    policy = AuctionPolicy(
        collection_window_seconds=60,
        preferred_providers=frozenset({"lisbon", "sofia", "helsinki"}),
        eligible_providers=frozenset({"lisbon", "sofia", "helsinki"}),
    )
    observations = [
        _bid("sofia", "20", 1),
        _bid("lisbon", "10", 30),
        _bid("helsinki", "15", 45),
    ]

    forward = _replay(policy, list(observations), now=60)
    reverse = _replay(policy, list(reversed(observations)), now=60)
    interleaved = _replay(
        policy,
        [observations[2], observations[0], observations[1]],
        now=60,
    )

    assert forward.selected == reverse.selected == interleaved.selected
    assert forward.selected is not None
    assert forward.selected.provider == "lisbon"
    assert forward.selection_reason == "cheapest_preferred"


def test_excluded_provider_never_wins_regardless_of_price_or_order():
    """An excluded provider is invisible to selection, even when cheapest.

    The library applies exclusion before ordering. Any consumer that ranks
    excluded providers into its own selector (e.g. a pre-filtered list fed
    to ``gpu_bid_planner``) will produce a different winner from this
    verdict — the divergence item 1 of #13 captures.
    """
    policy = AuctionPolicy(
        collection_window_seconds=30,
        preferred_providers=frozenset({"lisbon", "sofia"}),
        eligible_providers=frozenset({"lisbon", "sofia", "blocked"}),
        excluded_providers=frozenset({"blocked"}),
    )

    result = _replay(
        policy,
        [_bid("blocked", "1", 1), _bid("sofia", "5", 10), _bid("lisbon", "9", 20)],
        now=30,
    )

    assert result.selected is not None
    assert result.selected.provider == "sofia"
    assert any(
        item.provider == "blocked" and item.reason == "provider_excluded"
        for item in result.rejected
    )


def test_mixed_denominations_fail_closed_in_all_observation_orders():
    """A consumer must not silently pick one side of a denomination split.

    Bids in ``uact`` and ``uakt`` are not comparable. Any consumer that
    silently coerces or picks one side will diverge from this verdict.
    The library raises ``MixedBidDenominations``; consumers must propagate
    that signal rather than masking it.
    """
    policy = AuctionPolicy(collection_window_seconds=10)

    with_akt_first = [
        _bid("akt-provider", "1", 1, denom="uakt"),
        _bid("act-provider", "2", 2, denom="uact"),
    ]
    with_act_first = [
        _bid("act-provider", "2", 1, denom="uact"),
        _bid("akt-provider", "1", 2, denom="uakt"),
    ]

    for observations in (with_akt_first, with_act_first):
        try:
            _replay(policy, observations, now=10)
        except MixedBidDenominations:
            continue
        raise AssertionError(
            "expected MixedBidDenominations for a mixed-denom bid set; "
            "consumer that coerces silently will diverge from this verdict"
        )


# ---------------------------------------------------------------------------
# The conformance fixture, exported for replay by any adapter consumer
# ---------------------------------------------------------------------------


CONFORMANCE_FIXTURES: list[tuple[str, AuctionPolicy, list[BidObservation], float, str]] = [
    # (label, policy, observations, now, expected_provider)
    (
        "grace_window_preferred_late_beats_backup_early",
        _GRACE_POLICY,
        list(_GRACE_TIMELINE),
        60,
        "lisbon",
    ),
    (
        "no_preferred_bid_first_eligible_fallback_wins",
        AuctionPolicy(
            collection_window_seconds=30,
            preferred_providers=frozenset({"lisbon"}),
            eligible_providers=frozenset({"lisbon", "helsinki"}),
        ),
        # lisbon never bids; helsinki is the first observed eligible fallback
        [_bid("helsinki", "7", 11)],
        30,
        "helsinki",
    ),
    (
        "cheapest_preferred_wins_over_more_expensive_preferred",
        AuctionPolicy(
            collection_window_seconds=60,
            preferred_providers=frozenset({"lisbon", "sofia"}),
            eligible_providers=frozenset({"lisbon", "sofia"}),
        ),
        [_bid("sofia", "20", 1), _bid("lisbon", "10", 50)],
        60,
        "lisbon",
    ),
    (
        "exclusion_blocks_cheapest",
        AuctionPolicy(
            collection_window_seconds=30,
            preferred_providers=frozenset({"lisbon"}),
            eligible_providers=frozenset({"lisbon", "blocked"}),
            excluded_providers=frozenset({"blocked"}),
        ),
        [_bid("blocked", "1", 1), _bid("lisbon", "5", 20)],
        30,
        "lisbon",
    ),
]


def test_conformance_fixture_table_replays_to_expected_winners():
    """The shipped CONFORMANCE_FIXTURES table is the reference contract.

    Any downstream adapter (Console, wallet, compiler GPU burst, or a
    new one) that wants to claim it uses this library must replay this
    table and produce the same winner for every row. Adding a row here
    is a deliberate widening of the contract; removing or rewriting a row
    is a deliberate narrowing and requires updating every consumer.
    """
    for label, policy, observations, now, expected in CONFORMANCE_FIXTURES:
        result = _replay(policy, observations, now=now)
        assert result.selected is not None, label
        assert result.selected.provider == expected, (
            f"fixture {label!r} expected {expected!r}, got {result.selected.provider!r}"
        )
