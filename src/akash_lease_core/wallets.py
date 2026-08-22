"""Pure ranking for pre-measured Console wallet candidates.

Adapters own secrets, account discovery, balance reads, retries, and any
cross-process reservation. This module receives only non-secret normalized
snapshots and answers which funded account should be attempted first.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from decimal import Decimal
from enum import Enum


class WalletSelectionStatus(str, Enum):
    """Terminal result of a wallet ranking."""

    SELECTED = "selected"
    NO_FUNDED_WALLET = "no_funded_wallet"


@dataclass(frozen=True, slots=True)
class WalletCandidate:
    """One non-secret, measured Console account candidate."""

    candidate_id: str
    account: str
    available_credit: Decimal
    denom: str = "uact"

    def __post_init__(self) -> None:
        if not self.candidate_id:
            raise ValueError("candidate_id must not be empty")
        if not self.account:
            raise ValueError("account must not be empty")
        if not self.denom:
            raise ValueError("denom must not be empty")
        credit = (
            self.available_credit
            if isinstance(self.available_credit, Decimal)
            else Decimal(str(self.available_credit))
        )
        if not credit.is_finite() or credit < 0:
            raise ValueError("available_credit must be a finite non-negative number")
        object.__setattr__(self, "available_credit", credit)


@dataclass(frozen=True, slots=True)
class WalletPolicy:
    """Funding requirement for one operation."""

    required_credit: Decimal = Decimal(0)
    denom: str = "uact"
    version: str = "wallet-selection/v1"

    def __post_init__(self) -> None:
        required = (
            self.required_credit
            if isinstance(self.required_credit, Decimal)
            else Decimal(str(self.required_credit))
        )
        if not required.is_finite() or required < 0:
            raise ValueError("required_credit must be a finite non-negative number")
        if not self.denom:
            raise ValueError("denom must not be empty")
        if not self.version:
            raise ValueError("version must not be empty")
        object.__setattr__(self, "required_credit", required)


@dataclass(frozen=True, slots=True)
class RejectedWallet:
    """Machine-readable reason a wallet did not enter the funded ranking."""

    candidate_id: str
    account: str
    reason: str


@dataclass(frozen=True, slots=True)
class WalletSelectionResult:
    """Funded wallets in deterministic attempt order."""

    status: WalletSelectionStatus
    policy_version: str
    selected: WalletCandidate | None
    ranked: tuple[WalletCandidate, ...]
    rejected: tuple[RejectedWallet, ...]


def rank_wallets(
    candidates: Iterable[WalletCandidate], policy: WalletPolicy
) -> WalletSelectionResult:
    """Rank unique funded accounts by available credit, richest first.

    A second key resolving to the same account is not additional capacity and
    is rejected as a duplicate. Equal balances use account and candidate ID as
    stable tie-breakers, so input order cannot change the winner.
    """

    rejected: list[RejectedWallet] = []
    unique_by_account: dict[str, WalletCandidate] = {}
    for candidate in sorted(candidates, key=lambda item: (item.account, item.candidate_id)):
        if candidate.account in unique_by_account:
            rejected.append(_reject(candidate, "duplicate_account"))
            continue
        unique_by_account[candidate.account] = candidate

    funded: list[WalletCandidate] = []
    for candidate in unique_by_account.values():
        if candidate.denom != policy.denom:
            rejected.append(_reject(candidate, "wrong_denomination"))
        elif candidate.available_credit < policy.required_credit:
            rejected.append(_reject(candidate, "insufficient_credit"))
        else:
            funded.append(candidate)

    ranked = tuple(
        sorted(
            funded,
            key=lambda item: (-item.available_credit, item.account, item.candidate_id),
        )
    )
    selected = ranked[0] if ranked else None
    return WalletSelectionResult(
        status=(
            WalletSelectionStatus.SELECTED
            if selected is not None
            else WalletSelectionStatus.NO_FUNDED_WALLET
        ),
        policy_version=policy.version,
        selected=selected,
        ranked=ranked,
        rejected=tuple(rejected),
    )


def _reject(candidate: WalletCandidate, reason: str) -> RejectedWallet:
    return RejectedWallet(
        candidate_id=candidate.candidate_id,
        account=candidate.account,
        reason=reason,
    )
