"""Leaked-order sweep — decide whether an un-leased order is safe to close.

⛔ WHY THIS EXISTS: 99 active deployments held 484 ACT while total rent ever
burned was 1.97 ACT — 0.4%. Ninety-four of them were OPEN ORDERS: every group
still ``state == "open"``, meaning **no provider ever took the order**. Nothing
was ever deployed on them. They are pure escrow leak.

⭐ THE PREDICATE, and it is what makes this sweep safe at all:

    An order with NO LEASE has nothing running on it, whoever created it.

Closing it destroys no workload. So the "is this someone else's tenant
workload?" question — the one that blocked 480 ACT behind a name-based
protection for hours — **does not apply to this population**. That is not a
risk we are accepting; it is a question that is not posed.

⚠ It follows that the whole safety burden rests on ONE fact being right:
``lease_count == 0``. This module therefore refuses to infer that fact from a
weak instrument, and refuses to guess when instruments disagree.

FOUR INSTRUMENT DEFECTS, each encoded here as a REFUSAL rather than a comment
-----------------------------------------------------------------------------

1. **The Console LIST endpoint showed 51 of 99 deployments.** It is not an
   inventory. Enumeration belongs to the chain
   (``/akash/deployment/v1beta4/deployments/list?filters.owner=…``). This
   module cannot enumerate — it is sans-I/O — but it will not accept a
   list-sourced lease count: see ``LeaseEvidence.CONSOLE_LIST``.

2. **The Console list's lease data is wrong in BOTH directions** — measured:
   one dseq listed 1 lease and had 0; another listed 0 and had 1. A
   false 0 closes a LIVE deployment. So ``CONSOLE_LIST`` evidence yields
   ``UNDETERMINED``, always, even when it says zero. Being wrong in the safe
   direction some of the time is not a safety property.

3. **``pagination.total`` echoes the limit** (``limit=1`` -> ``total=1``), so
   ``skip >= total`` truncates the scan. ``page_is_last`` gives the only sound
   stop condition: a SHORT page.

4. **All Console API keys return the same account's deployments**, so an owner
   read from the list is meaningless. Owner must come from the chain; this
   module takes it as given and never derives it.

⚠ And ``escrow_account.state`` is a nested DICT — the flag is
``esc["state"]["state"]``, not ``esc["state"]``. That single bug corrupted both
the reporting AND the safety guard that was supposed to catch it; the guard
failed safe and skipped everything, which is the only reason it was noticed.
``parse_escrow_open`` exists so no consumer reaches into that shape by hand.

SAFETY RULES, learned by nearly breaking them
---------------------------------------------
* **AGE FLOOR — the load-bearing conjunct, proven twice INDEPENDENTLY.**
  Thirteen orders 1.6–2.3 MINUTES old were nearly closed once; forty minutes
  later a second operator reproduced the same population from scratch — the raw
  ``all groups open AND <family>`` predicate matched **17 orders / 85.00 ACT**,
  oldest 32.0 min, youngest 2.9 min. With the floor applied: **0**.
  ⇒ The bare predicate SELECTS LIVE CI MID-AUCTION. That was not one unlucky
  near-miss; it is what the predicate does without this conjunct. A sweep that
  closes on the predicate alone destroys running CI.
  The floor is DERIVED from the bid window (see ``BID_WINDOW_SECONDS``), not
  chosen, so it can be checked against the measurement rather than trusted.
* **RE-VERIFY IMMEDIATELY BEFORE THE DELETE.** One dseq went ACTIVE between the
  scan and the close. ``confirm_close`` exists to make the scan verdict
  non-binding: a batch decision is a CANDIDATE, never an authorisation.
* **EXCLUDE sibling-repo objects** on the shared wallet, and keep the explicit
  protected list.
* **BATCH CAP (default 20).**

⛔ THE SELECTOR IS ATTRIBUTION-FREE, AND MUST STAY THAT WAY
-----------------------------------------------------------
Only 2 of 67 live deployments carry an owner id at all (``dfci-infra-app``
0/50, ``consul`` 0/7). A close path keyed on ``ci_run_id`` or ``owner_scope``
would select **none** of the leaked orders — including the 14 measured
stranding escrow right now. So the predicate is deliberately
``all groups open`` + age + family + not-sibling, and nothing else. Stamping
identity at create time is a real fix, but it is UPSTREAM of this sweep, not a
dependency of it. Do not add an attribution term here to make the selector feel
more precise; it would make it select nothing.

⚠ ON RATES: 14 orders stranded in roughly 30 minutes is an EXISTENCE PROOF that
the create/close asymmetry is generating new leak, not a throughput figure. One
window cannot separate a burst from a steady state — the same reason a
two-sample allowance projection is unsound (see ``funding``). This module
therefore reports no rate and derives nothing from elapsed volume.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from enum import Enum

__all__ = [
    "BID_WINDOW_SECONDS",
    "BID_WINDOW_SAFETY_FACTOR",
    "DEFAULT_MIN_AGE_SECONDS",
    "DEFAULT_PROTECTED_DSEQS",
    "LeaseEvidence",
    "OrderDecision",
    "OrderObservation",
    "OrderPolicy",
    "OrderStatus",
    "confirm_close",
    "evaluate_order",
    "page_is_last",
    "parse_escrow_open",
    "select_batch",
]

# ⭐ THE FLOOR IS DERIVED, NOT CHOSEN. Do not replace it with a literal.
#
# Bids expire roughly 5 minutes after the ORDER opens, and the runner collects
# bids for ~450s. So an all-groups-`open` order older than its bid window can
# never acquire a lease: no new bid will arrive and any prior bid has expired.
# Past that point the order is definitively DEAD, not pending — which is the
# distinction that makes a short floor principled rather than merely brave.
BID_WINDOW_SECONDS = 450.0

# 2x the window. Not taste: it is the smallest multiple that clears the window
# with margin, and it costs ~15 minutes of stranded escrow instead of the ~2h an
# unexplained conservative floor was costing. A justified 15m beats an
# unexplained 2h — the reader can check this number against the measurement.
BID_WINDOW_SAFETY_FACTOR = 2.0

DEFAULT_MIN_AGE_SECONDS = BID_WINDOW_SECONDS * BID_WINDOW_SAFETY_FACTOR  # 900.0s

DEFAULT_PROTECTED_DSEQS = frozenset({"1784532174413"})

_KNOWN_GROUP_STATES = frozenset({"open", "active", "closed", "insufficient_funds", "paused"})


class LeaseEvidence(str, Enum):
    """WHERE the lease count came from. Not all sources may decide a close.

    ⛔ ``CONSOLE_LIST`` is measured-unreliable in BOTH directions and can never
    authorise a close, including when it reports zero.
    """

    CHAIN = "chain"
    DEPLOYMENT_DETAIL = "deployment_detail"
    CONSOLE_LIST = "console_list"

    @property
    def may_authorise_close(self) -> bool:
        return self in (LeaseEvidence.CHAIN, LeaseEvidence.DEPLOYMENT_DETAIL)


class OrderStatus(str, Enum):
    """⛔ SEVEN outcomes, and only ONE of them closes anything.

    ``UNDETERMINED`` is not a soft no — it is the value that keeps an
    unreadable instrument from being rendered as a safe-looking zero.
    """

    CLOSEABLE = "closeable"
    HAS_LEASE = "has_lease"
    TOO_YOUNG = "too_young"
    PROTECTED = "protected"
    EXCLUDED = "excluded"
    NOT_ACTIVE = "not_active"
    NOT_OPEN_ORDER = "not_open_order"
    UNDETERMINED = "undetermined"


@dataclass(frozen=True, slots=True)
class OrderObservation:
    """One reading of one order.

    Every ``None`` means UNREADABLE and must never be written as 0 or "".
    """

    dseq: str
    owner: str
    deployment_state: str | None
    lease_count: int | None
    lease_evidence: LeaseEvidence
    age_seconds: float | None
    group_states: tuple[str, ...] | None = None
    name: str = ""

    def __post_init__(self) -> None:
        if not self.dseq:
            raise ValueError("dseq must not be empty")
        if not self.owner:
            raise ValueError("owner must not be empty — it comes from the chain, never the list")
        if self.lease_count is not None:
            if isinstance(self.lease_count, bool) or not isinstance(self.lease_count, int):
                raise ValueError("lease_count must be an int or None (None = unreadable)")
            if self.lease_count < 0:
                raise ValueError("lease_count must be non-negative")
        if self.age_seconds is not None and self.age_seconds < 0:
            raise ValueError("age_seconds must be non-negative")


@dataclass(frozen=True, slots=True)
class OrderPolicy:
    """What this sweep is allowed to do."""

    min_age_seconds: float = DEFAULT_MIN_AGE_SECONDS
    batch_limit: int = 20
    protected_dseqs: frozenset[str] = field(default_factory=lambda: DEFAULT_PROTECTED_DSEQS)
    # ⚠ The FAMILY conjunct. Empty means "no family restriction", which is the
    # right default for a shared core but is NOT what a sweeper should run with:
    # the measured predicate is `all groups open AND <family> AND age AND
    # not-sibling`. A consumer sweeping a real wallet should set this.
    required_name_prefixes: tuple[str, ...] = ()
    excluded_name_prefixes: tuple[str, ...] = ("just-akash-",)
    excluded_owner_substrings: tuple[str, ...] = ("borduas",)
    version: str = "leaked-order-sweep/v1"

    def __post_init__(self) -> None:
        if self.min_age_seconds < 0:
            raise ValueError("min_age_seconds must be non-negative")
        if self.batch_limit < 1:
            raise ValueError("batch_limit must be >= 1")


@dataclass(frozen=True, slots=True)
class OrderDecision:
    dseq: str
    status: OrderStatus
    reason: str

    @property
    def closeable(self) -> bool:
        return self.status is OrderStatus.CLOSEABLE


def _excluded(obs: OrderObservation, policy: OrderPolicy) -> str | None:
    name = obs.name or ""
    for prefix in policy.excluded_name_prefixes:
        if name.startswith(prefix):
            return (
                f"name {name!r} matches excluded prefix {prefix!r} — "
                f"sibling-repo object on a shared wallet"
            )
    lowered = obs.owner.lower()
    for sub in policy.excluded_owner_substrings:
        if sub in lowered or sub in name.lower():
            return f"owner/name carries excluded token {sub!r} — not ours to close"
    return None


def evaluate_order(obs: OrderObservation, policy: OrderPolicy | None = None) -> OrderDecision:
    """Classify one order. ⚠ A CLOSEABLE verdict is a CANDIDATE, not an authorisation.

    Order of checks is deliberate: the refusals that do not depend on reading an
    instrument correctly come FIRST, so a misread cannot reach them.
    """
    policy = policy or OrderPolicy()

    if obs.dseq in policy.protected_dseqs:
        return OrderDecision(obs.dseq, OrderStatus.PROTECTED, "dseq is on the protected list")

    why = _excluded(obs, policy)
    if why is not None:
        return OrderDecision(obs.dseq, OrderStatus.EXCLUDED, why)

    if policy.required_name_prefixes and not any(
        (obs.name or "").startswith(pre) for pre in policy.required_name_prefixes
    ):
        return OrderDecision(
            obs.dseq,
            OrderStatus.EXCLUDED,
            f"name {obs.name!r} is outside the swept family {policy.required_name_prefixes}",
        )

    if not obs.lease_evidence.may_authorise_close:
        return OrderDecision(
            obs.dseq,
            OrderStatus.UNDETERMINED,
            f"lease count came from {obs.lease_evidence.value!r}, which is measured-wrong in BOTH "
            f"directions and cannot authorise a close even when it reports zero",
        )

    if obs.deployment_state is None:
        return OrderDecision(obs.dseq, OrderStatus.UNDETERMINED, "deployment state unreadable")
    if obs.deployment_state != "active":
        return OrderDecision(
            obs.dseq,
            OrderStatus.NOT_ACTIVE,
            f"deployment state is {obs.deployment_state!r}, nothing to close",
        )

    if obs.lease_count is None:
        return OrderDecision(
            obs.dseq, OrderStatus.UNDETERMINED, "lease count unreadable — never treat as 0"
        )
    if obs.lease_count > 0:
        return OrderDecision(
            obs.dseq,
            OrderStatus.HAS_LEASE,
            f"{obs.lease_count} lease(s) — a workload may be running",
        )

    # Corroboration, and the population definition. The leak family is
    # "every group still `open`" — no provider ever took the order.
    if obs.group_states is not None:
        unknown = tuple(g for g in obs.group_states if g not in _KNOWN_GROUP_STATES)
        if unknown:
            return OrderDecision(
                obs.dseq, OrderStatus.UNDETERMINED, f"unrecognised group state(s) {unknown}"
            )
        # An ACTIVE group with zero leases is two instruments disagreeing. That is
        # an unknown, never a tiebreak in favour of the convenient answer.
        contradicting = tuple(g for g in obs.group_states if g == "active")
        if contradicting:
            return OrderDecision(
                obs.dseq,
                OrderStatus.UNDETERMINED,
                "lease_count is 0 but a group reports active — instruments disagree",
            )
        if not obs.group_states or not all(g == "open" for g in obs.group_states):
            return OrderDecision(
                obs.dseq,
                OrderStatus.NOT_OPEN_ORDER,
                f"group states {obs.group_states} are not all open — not the leaked-order family",
            )

    if obs.age_seconds is None:
        return OrderDecision(
            obs.dseq, OrderStatus.UNDETERMINED, "age unreadable — the age floor cannot be applied"
        )
    if obs.age_seconds < policy.min_age_seconds:
        return OrderDecision(
            obs.dseq,
            OrderStatus.TOO_YOUNG,
            f"{obs.age_seconds:.0f}s old, floor is {policy.min_age_seconds:.0f}s "
            f"— may be mid-auction",
        )

    return OrderDecision(
        obs.dseq, OrderStatus.CLOSEABLE, "no lease, past the age floor, not protected or excluded"
    )


def select_batch(
    decisions: Iterable[OrderDecision], policy: OrderPolicy | None = None
) -> tuple[OrderDecision, ...]:
    """The closeable candidates, capped at the batch limit. Order is preserved."""
    policy = policy or OrderPolicy()
    out: list[OrderDecision] = []
    for d in decisions:
        if d.closeable:
            out.append(d)
            if len(out) >= policy.batch_limit:
                break
    return tuple(out)


def confirm_close(fresh: OrderObservation, policy: OrderPolicy | None = None) -> OrderDecision:
    """Re-check IMMEDIATELY before the delete, against a freshly-read observation.

    ⚠ This is not ceremony. One dseq went ACTIVE between the scan and the close;
    without this second read it would have killed a live deployment. The scan
    verdict is deliberately not an input here — passing it in would invite
    trusting it.
    """
    return evaluate_order(fresh, policy)


def parse_escrow_open(escrow: object) -> bool | None:
    """Is this escrow account open? ``None`` for any shape we do not recognise.

    ⚠ ``state`` is NESTED: ``{"state": {"state": "open"}}``. Reading
    ``escrow["state"]`` yields a dict, which is truthy for BOTH open and closed
    — the bug that broke the reporting and the safety guard together.
    """
    if not isinstance(escrow, dict):
        return None
    state = escrow.get("state")
    if isinstance(state, dict):
        state = state.get("state")
    if not isinstance(state, str) or not state:
        return None
    return state == "open"


def page_is_last(page: Sequence[object], limit: int) -> bool:
    """A page is the last one iff it is SHORT.

    ⛔ Do NOT stop on ``skip >= pagination.total``: ``total`` echoes the limit
    (``limit=1`` -> ``total=1``), so that condition truncates the scan after one
    page and reports a complete inventory.
    """
    if limit < 1:
        raise ValueError("limit must be >= 1")
    return len(page) < limit
