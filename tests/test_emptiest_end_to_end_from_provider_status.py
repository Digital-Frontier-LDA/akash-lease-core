"""THE JOIN: a real provider payload -> ProviderCapacity -> an emptiest auction.

⛔⛔ WHY A SEPARATE FILE, AND WHY IT IS THE TEST THAT MATTERED.
Both halves of this chain were already proven, independently:

    test_capacity_from_provider_status.py   status payload -> ProviderCapacity
    test_emptiest_selection.py              ProviderCapacity -> ranking

and NOTHING exercised the join. That is the shape where every test is green and
the value never travels: the halves agree with their own fixtures and disagree
with each other, and no suite is asking. A hand-built ``ProviderCapacity(cpu=0.92)``
in the ranking tests cannot notice that the parser emits a different unit, a
different dimension name, or ``None`` where the ranker expects a float.

THE FIXTURES ARE LIVE CAPTURES FROM THE THREE DFC PREFERRED PROVIDERS,
taken 2026-08-25, trimmed to the ``available.nodes`` this path reads:

    lisbon    20 nodes    68.1% free (binding)
    sofia      6 nodes    19.2% free
    helsinki   4 nodes    12.5% free

⭐ That spread is the operator's own premise, measured rather than assumed: one
datacenter is far larger than its siblings and sits well below them in
utilisation. Lisbon carries ~5.4x Helsinki's headroom.

⚠ These are SNAPSHOTS. The assertions below deliberately do NOT pin the exact
fractions -- a fixture recaptured next month would have different numbers and
the test would fail for being out of date rather than for being wrong. What is
pinned is the ORDERING and the mechanism, which is what the auction consumes.
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest

from akash_lease_core import from_provider_status
from akash_lease_core.auction import (
    Auction,
    AuctionPolicy,
    BidObservation,
    PreferredSelection,
)

FIXTURES = Path(__file__).parent / "fixtures"
PREFERRED = frozenset({"lisbon", "sofia", "helsinki"})


def _capacity(name: str):
    return from_provider_status(
        json.loads((FIXTURES / f"provider_status_{name}.json").read_text())
    )


def _auction(mode: PreferredSelection, fleet):
    policy = AuctionPolicy(
        collection_window_seconds=10,
        preferred_providers=PREFERRED,
        preferred_selection=mode,
    )
    auction = Auction(policy, started_at=0.0)
    for index, (provider, price) in enumerate(fleet):
        auction.observe(
            BidObservation(
                bid_key=f"bid-{index}",
                provider=provider,
                price=Decimal(price),
                denom="uakt",
                observed_at=1.0 + index,
                capacity=_capacity(provider),
            )
        )
    return auction


# The adversarial ordering: the emptiest provider is also the DEAREST, and it bids
# LAST. Cheapest-first and first-observed-first both pick someone else, so a pass
# here cannot come from either accidentally agreeing with emptiest.
FLEET = [("helsinki", "1"), ("sofia", "5"), ("lisbon", "9")]


def test_every_fixture_parses_into_a_readable_capacity() -> None:
    """⚠ Control. If a fixture stopped parsing, every ranking test below would
    fall back to cheapest and still look like a coherent result."""
    for name in ("lisbon", "sofia", "helsinki"):
        cap = _capacity(name)
        assert cap.is_readable, f"{name} fixture did not parse into a readable capacity"


def test_the_measured_headroom_ordering_is_lisbon_sofia_helsinki() -> None:
    """The operator's premise, as a property rather than as three magic numbers."""
    lisbon = _capacity("lisbon").available_fraction()
    sofia = _capacity("sofia").available_fraction()
    helsinki = _capacity("helsinki").available_fraction()
    assert lisbon > sofia > helsinki


def test_emptiest_selects_lisbon_from_REAL_provider_payloads() -> None:
    """⭐ The join. Nothing between the HTTP body and the winner is hand-built."""
    result = _auction(PreferredSelection.EMPTIEST, FLEET).evaluate(now=11.0)
    assert result.selected.provider == "lisbon"
    assert result.selection_reason == "emptiest_preferred"


def test_KNOWN_NEGATIVE_cheapest_picks_helsinki_from_the_same_payloads() -> None:
    """⛔ Without this the emptiest assertion proves nothing.

    Same fixtures, same bids, only the mode differs. Cheapest selects HELSINKI --
    the provider measured at 12.5% free, whose registry entry records a ~44%
    no-bid rate. That is the failure emptiest exists to prevent, and it is the
    current default.
    """
    result = _auction(PreferredSelection.CHEAPEST, FLEET).evaluate(now=11.0)
    assert result.selected.provider == "helsinki"
    assert result.selection_reason == "cheapest_preferred"


def test_an_unreadable_payload_degrades_to_cheapest_and_SAYS_SO() -> None:
    """⛔ A provider whose /status cannot be read must not be ranked as full.

    The capacity is None, so emptiest has nothing to rank on and falls back --
    and the reason string must report the fallback rather than claiming the mode
    it was asked for. An UNMEASURABLE fleet reporting 'emptiest_preferred' would
    be indistinguishable from a working one.
    """
    policy = AuctionPolicy(
        collection_window_seconds=10,
        preferred_providers=PREFERRED,
        preferred_selection=PreferredSelection.EMPTIEST,
    )
    auction = Auction(policy, started_at=0.0)
    for index, (provider, price) in enumerate(FLEET):
        auction.observe(
            BidObservation(
                bid_key=f"bid-{index}",
                provider=provider,
                price=Decimal(price),
                denom="uakt",
                observed_at=1.0 + index,
                capacity=from_provider_status({}),  # unreadable, NOT full
            )
        )
    result = auction.evaluate(now=11.0)
    assert result.selected.provider == "helsinki"  # cheapest, because nothing is rankable
    assert result.selection_reason != "emptiest_preferred"
    assert "emptiest" in result.selection_reason  # names the mode it could not honour


@pytest.mark.parametrize("name", ["sofia", "helsinki"])
def test_a_provider_with_no_GPUs_is_not_applicable_rather_than_full(name: str) -> None:
    """Sofia and Helsinki advertise 0 allocatable GPUs. That must be None.

    ⛔ If a zero total mapped to 0.0, both would rank as COMPLETELY FULL on the
    binding minimum, and emptiest would become a coin flip among providers whose
    only sin is having no GPUs.
    """
    assert _capacity(name).gpu is None


def test_idle_GPUs_do_not_inflate_a_provider_whose_CPU_is_the_constraint() -> None:
    """⭐ Measured on the real Lisbon payload, and the reason the fraction is a
    MINIMUM rather than a mean.

    Lisbon advertises 19 GPUs and every one of them is free, so its gpu
    dimension is 1.0 -- a perfect score on a resource the workload may not want.
    Its CPU is the scarce dimension at ~68%. The binding fraction must follow the
    CPU, not the GPUs: a mean across its four dimensions would report ~87% free
    and recommend a provider with a third less CPU headroom than advertised.
    """
    cap = _capacity("lisbon")
    assert cap.gpu == 1.0
    assert cap.available_fraction() == cap.cpu
    assert cap.available_fraction() < cap.memory
    assert cap.available_fraction() < cap.gpu
