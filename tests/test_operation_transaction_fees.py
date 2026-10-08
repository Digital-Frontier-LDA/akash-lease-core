"""Pure fee DATA proposals; actual fee attribution needs a registered adapter."""

from dataclasses import replace

import pytest
from test_operation_exposure import (
    NOW,
    commit,
    confirmed,
    initial,
    intent,
    observation,
    observed,
    registration,
    reserve,
    vector,
)

from akash_lease_core import canonical_journal_digest
from akash_lease_core.financial_codec import decode_financial_data, encode_financial_data
from akash_lease_core.operation_exposure import (
    FinancialEffectPurpose,
    FinancialEffectState,
    FinancialOperationState,
    FinancialTransitionError,
    NativeLiability,
    NativeLiabilityVector,
    financial_native_usage,
    propose_financial_observation,
)


def fee_intent(state, reg=None):
    fee = NativeLiabilityVector((NativeLiability("uakt", 2),))
    return intent(
        state.maximum,
        reg,
        now=NOW + 20,
        effect="close-fee",
        attempt="attempt:close-fee",
        purpose=FinancialEffectPurpose.TRANSACTION_FEE,
        debit=fee,
    )


def claimed_fee():
    state = confirmed()
    return reserve(state, fee_intent(state), now=NOW + 20)


def test_transaction_fee_retains_original_operation_leaf_mapping_and_lifetime_maximum():
    old = confirmed()
    state = reserve(old, fee_intent(old), now=NOW + 20)
    assert state.maximum is old.maximum
    assert state.maximum.creator_leaf == old.maximum.creator_leaf
    assert state.effects[-1].intent.subject == old.effects[0].intent.subject
    assert financial_native_usage(state) == vector(30, 4)
    assert decode_financial_data(encode_financial_data(state), FinancialOperationState) == state
    assert not hasattr(state, "sign") and not hasattr(state, "financial_release")


@pytest.mark.parametrize("state", [initial(), reserve()])
def test_transaction_fee_requires_a_confirmed_original_create(state):
    with pytest.raises(FinancialTransitionError, match="mapping"):
        reserve(state, fee_intent(state), now=NOW + 20)


@pytest.mark.parametrize("debit", [vector(1, 2), vector(0, 3)])
def test_transaction_fee_cannot_hide_deposit_or_larger_fee_liability(debit):
    with pytest.raises(ValueError, match="exact native fee vector"):
        replace(fee_intent(confirmed()), maximum_debit=debit)


def test_fee_actor_needs_explicit_purpose_registration():
    state = confirmed()
    reg = registration(state.maximum)
    reg = replace(
        reg,
        purposes=tuple(p for p in reg.purposes if p is not FinancialEffectPurpose.TRANSACTION_FEE),
    )
    with pytest.raises(FinancialTransitionError, match="cannot perform"):
        reserve(state, fee_intent(state, reg), reg, now=NOW + 20)


def test_unknown_fee_remains_charged_and_cannot_be_signed_again_after_recovery():
    state = claimed_fee()
    recovered = decode_financial_data(encode_financial_data(state), FinancialOperationState)
    assert financial_native_usage(recovered) == vector(30, 4)
    with pytest.raises(FinancialTransitionError, match="reconcile"):
        reserve(recovered, recovered.effects[-1].intent, now=NOW + 20)


@pytest.mark.parametrize(
    "terminal,code", [(FinancialEffectState.CONFIRMED, 0), (FinancialEffectState.FAILED, 5)]
)
def test_confirmed_and_failed_fee_charge_remains_with_original_operation(terminal, code):
    state = claimed_fee()
    item = state.effects[-1].intent
    for phase in (
        FinancialEffectState.SIGNED,
        FinancialEffectState.BROADCAST_CLAIMED_UNKNOWN,
        terminal,
    ):
        evidence = observation(
            item,
            phase,
            now=NOW + 20,
            debit=item.fee_debit if phase is terminal else None,
            code=code if phase is terminal else None,
            obs=observed(now=NOW + 20),
        )
        state = propose_financial_observation(
            state, item.effect_id, evidence, commit(state, now=NOW + 20), now=NOW + 20
        )
    assert state.effects[-1].state is terminal
    assert financial_native_usage(state) == vector(30, 4)
    assert canonical_journal_digest(state.maximum) == item.quote_digest


def test_fee_intent_cannot_change_original_deployment_or_exceed_lifetime_maximum():
    state = confirmed()
    with pytest.raises(ValueError, match="deployment"):
        reserve(
            state,
            replace(
                fee_intent(state), subject=replace(state.effects[0].intent.subject, dseq="9002")
            ),
            now=NOW + 20,
        )
    oversized = NativeLiabilityVector((NativeLiability("uakt", 21),))
    item = replace(fee_intent(state), maximum_debit=oversized, fee_debit=oversized)
    with pytest.raises(ValueError, match="maximum"):
        reserve(state, item, now=NOW + 20)
