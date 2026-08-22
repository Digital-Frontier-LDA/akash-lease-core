from decimal import Decimal

import pytest

from akash_lease_core import WalletCandidate, WalletPolicy, WalletSelectionStatus, rank_wallets


def wallet(candidate_id: str, account: str, credit: str, denom: str = "uact"):
    return WalletCandidate(
        candidate_id=candidate_id,
        account=account,
        available_credit=Decimal(credit),
        denom=denom,
    )


def test_ranks_highest_available_credit_first():
    result = rank_wallets(
        [wallet("key-a", "account-a", "5"), wallet("key-b", "account-b", "90")],
        WalletPolicy(required_credit=Decimal("5")),
    )

    assert result.status is WalletSelectionStatus.SELECTED
    assert result.selected is not None
    assert result.selected.account == "account-b"
    assert [item.account for item in result.ranked] == ["account-b", "account-a"]


def test_rejects_wallets_below_the_required_deployment_credit():
    result = rank_wallets(
        [wallet("key-a", "account-a", "4.99"), wallet("key-b", "account-b", "0")],
        WalletPolicy(required_credit=Decimal("5")),
    )

    assert result.status is WalletSelectionStatus.NO_FUNDED_WALLET
    assert result.selected is None
    assert {item.reason for item in result.rejected} == {"insufficient_credit"}


def test_duplicate_keys_for_one_account_do_not_create_fake_capacity():
    result = rank_wallets(
        [wallet("key-z", "same-account", "50"), wallet("key-a", "same-account", "50")],
        WalletPolicy(),
    )

    assert len(result.ranked) == 1
    assert result.selected is not None
    assert result.selected.candidate_id == "key-a"
    assert result.rejected[0].candidate_id == "key-z"
    assert result.rejected[0].reason == "duplicate_account"


def test_ties_are_stable_by_account_then_candidate_id():
    candidates = [wallet("z", "account-z", "20"), wallet("a", "account-a", "20")]

    assert (
        rank_wallets(candidates, WalletPolicy()).selected
        == rank_wallets(list(reversed(candidates)), WalletPolicy()).selected
    )
    assert rank_wallets(candidates, WalletPolicy()).selected.account == "account-a"


def test_wrong_denomination_is_rejected_not_compared():
    result = rank_wallets(
        [wallet("akt", "account-a", "999", "uakt"), wallet("act", "account-b", "5")],
        WalletPolicy(required_credit=Decimal("1"), denom="uact"),
    )

    assert result.selected is not None
    assert result.selected.account == "account-b"
    assert result.rejected[0].reason == "wrong_denomination"


@pytest.mark.parametrize("credit", ["-1", "NaN", "Infinity"])
def test_candidate_credit_must_be_finite_and_non_negative(credit):
    with pytest.raises(ValueError, match="available_credit"):
        wallet("key", "account", credit)


def test_policy_required_credit_must_be_finite_and_non_negative():
    with pytest.raises(ValueError, match="required_credit"):
        WalletPolicy(required_credit=Decimal("-1"))


def test_wallet_contract_is_exported_from_package_root():
    from akash_lease_core import rank_wallets as root_rank_wallets

    assert root_rank_wallets is rank_wallets
