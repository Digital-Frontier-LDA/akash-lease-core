"""A count written in prose must match the members it counts.

⛔ MEASURED 2026-08-29. ``OrderStatus`` carried eight members under a docstring reading
"⛔ SEVEN outcomes, and only ONE of them closes anything." The prose had not been counted
again after a member was added.

⚠ WHY A STALE COUNT MATTERS HERE SPECIFICALLY, rather than being cosmetic: this enum is
the authority that downstream consumers derive from. just-akash's `summarise()` seeds its
counts from it and its CLI iterates it. A reader auditing "are all the statuses handled?"
against the number in the docstring would conclude either that a complete handler was
missing one, or that an incomplete one was complete. The number is load-bearing for
exactly the review this class invites.

⚠ ANTI-VACUITY: this test must fail when the two disagree, not merely when the word is
absent. A docstring with no number at all is also a failure — silence is how a claim
avoids being checked.
"""

from __future__ import annotations

import re

from akash_lease_core.orders import OrderStatus

_WORDS = {
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
    "eleven": 11,
    "twelve": 12,
}


def _claimed_outcome_count(doc: str) -> int | None:
    """The number the prose claims, as a word or a digit, before 'outcomes'."""
    m = re.search(r"\b(\w+)\s+outcomes\b", doc, re.IGNORECASE)
    if not m:
        return None
    token = m.group(1).lower()
    if token.isdigit():
        return int(token)
    return _WORDS.get(token)


def test_status_docstring_count_matches_the_enum():
    doc = OrderStatus.__doc__ or ""
    claimed = _claimed_outcome_count(doc)
    actual = len(list(OrderStatus))
    assert claimed is not None, (
        "OrderStatus' docstring states no outcome count. Silence is how a claim avoids "
        f"being checked — say the number so it can be verified. Members: {actual}"
    )
    assert claimed == actual, (
        f"OrderStatus' docstring claims {claimed} outcomes; the enum has {actual}: "
        f"{[s.value for s in OrderStatus]}. Three consumers derive from this enum, so a "
        "stale count misleads exactly the completeness review the docstring invites."
    )


def test_the_check_can_see_a_mismatch():
    """ANTI-VACUITY: the parser must actually read the number, not always return None."""
    assert _claimed_outcome_count("⛔ SEVEN outcomes, and only ONE closes anything.") == 7
    assert _claimed_outcome_count("EIGHT outcomes") == 8
    assert _claimed_outcome_count("12 outcomes") == 12
    assert _claimed_outcome_count("no number here") is None


def test_only_one_status_closes_anything():
    """The docstring's OTHER claim — 'only ONE of them closes anything' — is testable too."""
    from akash_lease_core.orders import OrderDecision

    closes = [s for s in OrderStatus if OrderDecision("d", s, "why").closeable]
    assert len(closes) == 1, f"docstring says exactly one status closes; got {closes}"
    assert closes[0] is OrderStatus.CLOSEABLE
