"""Pure financial DATA controls; fixtures are not production acquisition proof."""

import hashlib
import json
from dataclasses import replace

import pytest

from akash_lease_core import (
    AdmissionScope,
    BackendIdentity,
    BackendKind,
    DeploymentKey,
    LifecycleIdentity,
    OwnerCandidate,
    OwnerEvidenceKind,
    PermitRevocationStatus,
    PreparedCreate,
    PreparedGroup,
    ProducerProvenance,
    canonical_group_population_digest,
    canonical_journal_bytes,
    canonical_journal_digest,
    decode_admission_state,
    decode_prepared_create,
    financial_codec,
)
from akash_lease_core.financial_codec import (
    MAX_FINANCIAL_RECOVERY_BYTES,
    FinancialDecodeError,
    decode_financial_data,
    encode_financial_data,
)
from akash_lease_core.operation_exposure import (
    FinancialCommitBinding,
    FinancialEffectIntent,
    FinancialEffectObservation,
    FinancialEffectPurpose,
    FinancialEffectState,
    FinancialOperationState,
    FinancialTransitionError,
    FundingActorRegistration,
    NativeLiability,
    NativeLiabilityVector,
    OperationMaximumLiability,
    QualifiedUactBasis,
    ServicePrincipalBinding,
    ServicePrincipalObservation,
    add_native_liabilities,
    financial_native_usage,
    propose_financial_effect,
    propose_financial_observation,
    propose_operation_maximum,
    validate_financial_commit_binding,
)

OWNER = "akash1n4uut3vxmkdp8wsrya3q0qyddgqey0rh9as4ee"
FOREIGN = "akash1cklqag0e0v794msgqkr6uwf7sr6zyy42gm78f0"
NOW = 1_800_000_000
POLICY = "9" * 40


def vector(uact=100, uakt=20):
    return NativeLiabilityVector((NativeLiability("uact", uact), NativeLiability("uakt", uakt)))


def maximum():
    leaf = AdmissionScope(
        "akashnet-2",
        OWNER,
        "https://token.actions.githubusercontent.com",
        "456",
        "123",
        "example/repo",
        "ci-runner",
    )
    groups = (PreparedGroup(1, "ci-runner-group"),)
    prepared = PreparedCreate(
        "create:1",
        1,
        ProducerProvenance(
            "example/repo",
            "123",
            "456",
            leaf.producer_issuer,
            "akash-create",
            "repo:example/repo:ref:refs/heads/main",
            "example/repo/.github/workflows/ci.yml@refs/heads/main",
            "1" * 40,
            "refs/heads/main",
            "nonce",
            "2" * 64,
        ),
        LifecycleIdentity("example/repo", "ci-runner", run=42, run_attempt=1),
        OwnerCandidate(
            BackendIdentity(BackendKind.MEDIATED, "console:fixture"),
            OWNER,
            OwnerEvidenceKind.AUTHENTICATED_MEDIATOR,
            "fixture",
            "3" * 64,
        ),
        "4" * 64,
        "5" * 64,
        groups,
        canonical_group_population_digest(groups),
        "6" * 40,
        NOW - 2,
    )
    return OperationMaximumLiability(
        "quote:1",
        prepared,
        leaf,
        "owner-store:fixture",
        POLICY,
        "a" * 64,
        vector(),
        None,
        "b" * 64,
        NOW - 1,
        NOW + 10,
    )


def principal():
    return ServicePrincipalBinding(
        "https://service-issuer.example",
        "console:autofunder",
        "akash-financial-broker",
        "c" * 64,
        "d" * 40,
        "e" * 64,
    )


def registration(q=None):
    q = q or maximum()
    return FundingActorRegistration(
        "managed-funder",
        q.creator_leaf.chain_id,
        OWNER,
        q.prepared.owner_candidate.backend,
        principal(),
        q.cas_domain,
        POLICY,
        q.writer_population_digest,
        (canonical_journal_digest(q.creator_leaf),),
        (OWNER,),
        tuple(sorted(FinancialEffectPurpose, key=lambda p: p.value)),
        "f" * 64,
        NOW - 1,
        NOW + 100,
    )


def observed(reg=None, now=NOW):
    reg = reg or registration()
    return ServicePrincipalObservation(
        reg.principal, 3, PermitRevocationStatus.ACTIVE, "0" * 64, now, now + 5
    )


def intent(
    q=None,
    reg=None,
    *,
    now=NOW,
    effect="initial",
    attempt="attempt:1",
    purpose=FinancialEffectPurpose.CREATE,
    debit=None,
):
    q = q or maximum()
    reg = reg or registration(q)
    return FinancialEffectIntent(
        q.prepared.operation_id,
        effect,
        attempt,
        canonical_journal_digest(q),
        canonical_journal_digest(reg),
        reg.principal,
        3,
        DeploymentKey(OWNER, "9001"),
        OWNER,
        purpose,
        "1" * 64,
        hashlib.sha256(effect.encode()).hexdigest(),
        "3" * 64,
        7 if effect == "initial" else 8,
        debit or vector(30, 2),
        NativeLiabilityVector((NativeLiability("uakt", 2),)),
        "4" * 64,
        now,
    )


def commit(state, *, now=NOW):
    q = state.maximum
    return FinancialCommitBinding(
        q.cas_domain,
        q.creator_leaf.chain_id,
        OWNER,
        q.prepared.operation_id,
        state.revision + 1,
        "5" * 64,
        state.revision,
        canonical_journal_digest(state),
        "6" * 64,
        now,
    )


def initial():
    return propose_operation_maximum(maximum(), now=NOW)


def reserve(state=None, item=None, reg=None, obs=None, *, now=NOW):
    state = state or initial()
    reg = reg or registration(state.maximum)
    item = item or intent(state.maximum, reg, now=now)
    return propose_financial_effect(
        state, item, reg, obs or observed(reg, now), commit(state, now=now), now=now
    )


def observation(item, kind, *, now=NOW, debit=None, tx="7" * 64, code=None, obs=None):
    return FinancialEffectObservation(
        canonical_journal_digest(item),
        kind,
        "8" * 64,
        now,
        None if kind is FinancialEffectState.PROVEN_NOT_SIGNED else tx,
        debit,
        code,
        obs if kind is FinancialEffectState.BROADCAST_CLAIMED_UNKNOWN else None,
    )


def advance(state, kind, *, now=NOW, debit=None, code=None, obs=None):
    item = state.effects[0].intent
    evidence = observation(
        item, kind, now=now, debit=debit, code=code, obs=obs or observed(now=now)
    )
    return propose_financial_observation(
        state, item.effect_id, evidence, commit(state, now=now), now=now
    )


def confirmed():
    state = advance(reserve(), FinancialEffectState.SIGNED)
    state = advance(state, FinancialEffectState.BROADCAST_CLAIMED_UNKNOWN)
    return advance(state, FinancialEffectState.CONFIRMED, debit=vector(30, 2), code=0)


def test_two_phase_maximum_proposal_and_commit_are_data_not_authority():
    state = initial()
    assert state.effects == () and state.revision == 0
    assert validate_financial_commit_binding(state, commit(state)) is None
    assert not hasattr(state, "sign") and not hasattr(commit(state), "authorize")
    assert not hasattr(state.maximum, "activate")
    assert state.maximum.uact_basis is None  # No guessed currency equivalence.


def test_full_maximum_is_retained_once_on_original_leaf_across_funding():
    state = confirmed()
    prior_maximum = state.maximum
    funding = intent(
        state.maximum,
        now=NOW + 20,
        effect="topup",
        attempt="attempt:2",
        purpose=FinancialEffectPurpose.DEPOSIT,
        debit=vector(40, 2),
    )
    later = reserve(state, funding, now=NOW + 20)
    assert later.maximum is prior_maximum
    assert later.maximum.creator_leaf == maximum().creator_leaf
    assert financial_native_usage(later) == vector(70, 4)
    assert not hasattr(later, "active_deployments")
    assert not hasattr(later, "financial_release")


@pytest.mark.parametrize("phase", list(FinancialEffectState))
def test_retained_attempt_cannot_sign_again_in_any_phase(phase):
    state = reserve()
    if phase is not FinancialEffectState.SIGN_RESERVED_UNKNOWN:
        if phase is FinancialEffectState.PROVEN_NOT_SIGNED:
            state = advance(state, phase)
        else:
            state = advance(state, FinancialEffectState.SIGNED)
            if phase is not FinancialEffectState.SIGNED:
                state = advance(state, FinancialEffectState.BROADCAST_CLAIMED_UNKNOWN)
                if phase in (FinancialEffectState.CONFIRMED, FinancialEffectState.FAILED):
                    state = advance(
                        state,
                        phase,
                        debit=vector(30, 2),
                        code=0 if phase is FinancialEffectState.CONFIRMED else 5,
                    )
    recovered = decode_financial_data(encode_financial_data(state), FinancialOperationState)
    with pytest.raises(FinancialTransitionError, match="reconcile"):
        reserve(recovered, recovered.effects[0].intent)
    assert recovered == state


@pytest.mark.parametrize(
    "phase",
    [
        FinancialEffectState.SIGN_RESERVED_UNKNOWN,
        FinancialEffectState.SIGNED,
        FinancialEffectState.BROADCAST_CLAIMED_UNKNOWN,
    ],
)
def test_unknown_restart_missing_private_signed_bytes_keeps_full_charge(phase):
    state = reserve()
    if phase is not FinancialEffectState.SIGN_RESERVED_UNKNOWN:
        state = advance(state, FinancialEffectState.SIGNED)
    if phase is FinancialEffectState.BROADCAST_CLAIMED_UNKNOWN:
        state = advance(state, phase)
    recovered = decode_financial_data(encode_financial_data(state), FinancialOperationState)
    assert financial_native_usage(recovered) == vector(30, 2)
    assert b"signed_bytes" not in encode_financial_data(state)
    assert recovered.maximum.native_maximum == vector()


def test_failed_transaction_still_charges_declared_fees():
    state = advance(reserve(), FinancialEffectState.SIGNED)
    state = advance(state, FinancialEffectState.BROADCAST_CLAIMED_UNKNOWN)
    state = advance(
        state,
        FinancialEffectState.FAILED,
        debit=NativeLiabilityVector((NativeLiability("uakt", 2),)),
        code=5,
    )
    assert financial_native_usage(state).amount("uakt") == 2
    assert state.maximum.native_maximum == vector()


def test_failed_transaction_cannot_zero_out_fees():
    state = advance(
        advance(reserve(), FinancialEffectState.SIGNED),
        FinancialEffectState.BROADCAST_CLAIMED_UNKNOWN,
    )
    with pytest.raises(FinancialTransitionError):
        advance(state, FinancialEffectState.FAILED, debit=NativeLiabilityVector(()), code=5)


@pytest.mark.parametrize("amount", [-1, True, False, 1.0, "1", None, float("inf"), float("nan")])
def test_native_quantities_reject_noncanonical_numbers(amount):
    with pytest.raises(ValueError):
        NativeLiability("uact", amount)


@pytest.mark.parametrize("names", [("uakt", "uact"), ("uact", "uact")])
def test_native_vector_requires_complete_sorted_unique_denominations(names):
    with pytest.raises(ValueError):
        NativeLiabilityVector(tuple(NativeLiability(n, 1) for n in names))


def test_native_vectors_preserve_names_zero_and_arbitrary_integer_precision():
    amount = 2**256
    native = NativeLiabilityVector(
        (
            NativeLiability("1custom", 0),
            NativeLiability("ibc/A", amount),
            NativeLiability("原生", 3),
        )
    )
    assert decode_financial_data(encode_financial_data(native), NativeLiabilityVector) == native
    assert add_native_liabilities((native, native)).amount("ibc/A") == amount * 2
    assert not vector().covers(NativeLiabilityVector((NativeLiability("UACT", 1),)))
    assert not vector().covers(NativeLiabilityVector((NativeLiability("foreign", 0),)))


@pytest.mark.parametrize("name", ["", "uact ", " uact", "a\x00", "a\n", "a" * 257])
def test_native_encoding_bounds_do_not_normalize_bad_names(name):
    with pytest.raises(ValueError):
        NativeLiability(name, 1)


def test_qualified_uact_basis_requires_exact_native_maximum_and_policy():
    q = maximum()
    basis = QualifiedUactBasis(
        q.native_maximum, 200, POLICY, "1" * 64, "2" * 64, "fixture-policy", NOW - 1, NOW + 5
    )
    qualified = replace(q, uact_basis=basis)
    assert propose_operation_maximum(qualified, now=NOW).maximum.uact_basis == basis
    for wrong in (
        replace(basis, native_maximum=vector(99, 20)),
        replace(basis, policy_revision="8" * 40),
    ):
        with pytest.raises(ValueError):
            replace(q, uact_basis=wrong)
    with pytest.raises(FinancialTransitionError):
        propose_operation_maximum(
            replace(q, uact_basis=replace(basis, valid_until=NOW - 1)), now=NOW
        )


@pytest.mark.parametrize(
    "field,value",
    [
        ("owner", FOREIGN),
        ("repository_id", "999"),
        ("repository_owner_id", "999"),
        ("repository", "other/repo"),
        ("producer_issuer", "https://foreign.example"),
        ("workload_class", "prod-payload"),
    ],
)
def test_maximum_cannot_relabel_original_creator_leaf(field, value):
    q = maximum()
    with pytest.raises(ValueError):
        replace(q, creator_leaf=replace(q.creator_leaf, **{field: value}))


@pytest.mark.parametrize(
    "field",
    [
        "issuer",
        "subject",
        "audience",
        "credential_fingerprint",
        "deployed_source_revision",
        "registration_digest",
    ],
)
def test_changed_service_identity_is_not_original_creator_provenance(field):
    reg = registration()
    replacement = (
        "a" * (40 if field == "deployed_source_revision" else 64)
        if "digest" in field or "fingerprint" in field or "revision" in field
        else "foreign-identity"
    )
    altered = replace(reg.principal, **{field: replacement})
    with pytest.raises(FinancialTransitionError):
        reserve(item=replace(intent(), principal=altered))
    assert not hasattr(reg.principal, "repository_id")


@pytest.mark.parametrize(
    "field,value",
    [
        ("status", PermitRevocationStatus.REVOKED),
        ("revocation_epoch", 4),
        ("observed_at", NOW + 1),
        ("valid_until", NOW - 1),
    ],
)
def test_stale_revoked_or_changed_current_principal_holds(field, value):
    obs = observed()
    if field == "valid_until":
        obs = replace(obs, observed_at=NOW - 2)
    with pytest.raises(FinancialTransitionError):
        reserve(obs=replace(obs, **{field: value}))


@pytest.mark.parametrize(
    "field,value",
    [
        ("policy_revision", "1" * 40),
        ("writer_population_digest", "1" * 64),
        ("cas_domain", "different-domain"),
        ("chain_id", "other-chain"),
        ("owner", FOREIGN),
    ],
)
def test_wrong_policy_owner_registry_or_domain_holds(field, value):
    reg = replace(registration(), **{field: value})
    with pytest.raises(FinancialTransitionError):
        reserve(reg=reg, item=intent(reg=reg))


@pytest.mark.parametrize("which", ["future", "stale"])
def test_fresh_actor_policy_registration_is_required(which):
    reg = registration()
    reg = (
        replace(reg, observed_at=NOW + 1)
        if which == "future"
        else replace(reg, observed_at=NOW - 2, valid_until=NOW - 1)
    )
    with pytest.raises(FinancialTransitionError):
        reserve(reg=reg, item=intent(reg=reg))


@pytest.mark.parametrize(
    "field,value",
    [
        ("cas_domain", "foreign"),
        ("chain_id", "foreign"),
        ("owner", FOREIGN),
        ("operation_id", "foreign"),
        ("financial_revision", 1),
        ("financial_state_digest", "f" * 64),
    ],
)
def test_ack_data_must_bind_exact_retained_financial_pointer(field, value):
    state = initial()
    with pytest.raises(FinancialTransitionError):
        validate_financial_commit_binding(state, replace(commit(state), **{field: value}))


def test_old_ack_cannot_propose_new_effect_or_broadcast_after_state_changes():
    state = initial()
    old = commit(state)
    signed = advance(reserve(state), FinancialEffectState.SIGNED)
    with pytest.raises(FinancialTransitionError):
        propose_financial_observation(
            signed,
            "initial",
            observation(
                signed.effects[0].intent,
                FinancialEffectState.BROADCAST_CLAIMED_UNKNOWN,
                obs=observed(),
            ),
            old,
            now=NOW,
        )


@pytest.mark.parametrize(
    "kind",
    [
        FinancialEffectState.BROADCAST_CLAIMED_UNKNOWN,
        FinancialEffectState.CONFIRMED,
        FinancialEffectState.FAILED,
    ],
)
def test_cannot_skip_signing_and_send_claim_history(kind):
    state = reserve()
    with pytest.raises(FinancialTransitionError):
        advance(
            state,
            kind,
            debit=vector(30, 2)
            if kind in (FinancialEffectState.CONFIRMED, FinancialEffectState.FAILED)
            else None,
            code=0
            if kind is FinancialEffectState.CONFIRMED
            else 5
            if kind is FinancialEffectState.FAILED
            else None,
        )


@pytest.mark.parametrize(
    "field,value",
    [("transaction_hash", "a" * 64), ("intent_digest", "a" * 64), ("observed_at", NOW - 1)],
)
def test_cannot_change_signed_transaction_or_intent_or_rewind_time(field, value):
    state = advance(reserve(), FinancialEffectState.SIGNED)
    evidence = observation(
        state.effects[0].intent, FinancialEffectState.BROADCAST_CLAIMED_UNKNOWN, obs=observed()
    )
    with pytest.raises(FinancialTransitionError):
        propose_financial_observation(
            state, "initial", replace(evidence, **{field: value}), commit(state), now=NOW
        )


@pytest.mark.parametrize(
    "field,value",
    [
        ("status", PermitRevocationStatus.REVOKED),
        ("revocation_epoch", 4),
        ("principal", replace(principal(), subject="foreign")),
    ],
)
def test_broadcast_claim_requires_exact_current_principal_data(field, value):
    state = advance(reserve(), FinancialEffectState.SIGNED)
    with pytest.raises(FinancialTransitionError):
        advance(
            state,
            FinancialEffectState.BROADCAST_CLAIMED_UNKNOWN,
            obs=replace(observed(), **{field: value}),
        )


def test_duplicate_broadcast_claim_and_lookup_absence_do_not_allow_replay():
    state = advance(
        advance(reserve(), FinancialEffectState.SIGNED),
        FinancialEffectState.BROADCAST_CLAIMED_UNKNOWN,
    )
    recovered = decode_financial_data(encode_financial_data(state), FinancialOperationState)
    with pytest.raises(FinancialTransitionError):
        advance(recovered, FinancialEffectState.BROADCAST_CLAIMED_UNKNOWN)
    with pytest.raises(FinancialTransitionError):
        advance(recovered, FinancialEffectState.PROVEN_NOT_SIGNED)


@pytest.mark.parametrize("amount", [71, 101, 2**64])
def test_full_remaining_lifetime_cap_is_enforced(amount):
    state = confirmed()
    item = intent(
        state.maximum,
        purpose=FinancialEffectPurpose.DEPOSIT,
        effect="topup",
        attempt="attempt:2",
        debit=vector(amount, 2),
    )
    with pytest.raises(ValueError, match="maximum"):
        reserve(state, item)


@pytest.mark.parametrize(
    "purpose", [FinancialEffectPurpose.AMENDMENT, FinancialEffectPurpose.DEPOSIT]
)
def test_no_initial_funding_or_cap_amendment_without_original_mapping(purpose):
    with pytest.raises(FinancialTransitionError):
        reserve(item=intent(purpose=purpose))


def test_replacement_create_and_foreign_deployment_cannot_reuse_allocation():
    state = confirmed()
    for item in (
        intent(effect="replacement", attempt="attempt:2"),
        replace(
            intent(effect="topup", attempt="attempt:2", purpose=FinancialEffectPurpose.DEPOSIT),
            subject=DeploymentKey(OWNER, "9002"),
        ),
    ):
        with pytest.raises((ValueError, FinancialTransitionError)):
            reserve(state, item)


@pytest.mark.parametrize("code", [True, -1, 2**32, "0", 0.0])
def test_chain_code_is_strict_uint32_and_fee_failure_is_not_success(code):
    with pytest.raises(ValueError):
        observation(intent(), FinancialEffectState.CONFIRMED, debit=vector(30, 2), code=code)


@pytest.mark.parametrize("phase", [FinancialEffectState.CONFIRMED, FinancialEffectState.FAILED])
def test_confirmed_chain_code_matches_outcome(phase):
    with pytest.raises(ValueError):
        observation(
            intent(),
            phase,
            debit=vector(30, 2),
            code=5 if phase is FinancialEffectState.CONFIRMED else 0,
        )


@pytest.mark.parametrize(
    "root",
    [
        NativeLiabilityVector,
        ServicePrincipalBinding,
        FundingActorRegistration,
        OperationMaximumLiability,
        FinancialEffectIntent,
        FinancialOperationState,
        FinancialCommitBinding,
    ],
)
def test_all_financial_data_roots_round_trip_without_capability(root):
    samples = {
        NativeLiabilityVector: vector(),
        ServicePrincipalBinding: principal(),
        FundingActorRegistration: registration(),
        OperationMaximumLiability: maximum(),
        FinancialEffectIntent: intent(),
        FinancialOperationState: confirmed(),
        FinancialCommitBinding: commit(initial()),
    }
    raw = encode_financial_data(samples[root])
    restored = decode_financial_data(raw, root)
    assert restored == samples[root] and encode_financial_data(restored) == raw


@pytest.mark.parametrize(
    "mutation",
    [
        "duplicate",
        "extra",
        "missing",
        "tag",
        "version",
        "bool",
        "float",
        "nan",
        "string",
        "whitespace",
    ],
)
def test_strict_codec_rejects_aliases_unknown_tags_and_noncanonical_bytes(mutation):
    raw = encode_financial_data(vector())
    if mutation == "duplicate":
        raw = raw.replace(b'"amount":100', b'"amount":100,"amount":100', 1)
    elif mutation == "whitespace":
        raw += b"\n"
    else:
        value = json.loads(raw)
        if mutation == "extra":
            value["authority"] = True
        elif mutation == "missing":
            del value["quantities"]
        elif mutation == "tag":
            value["$type"] = "FinancialSignAuthorization"
        elif mutation == "version":
            value["schema_version"] = 2
        else:
            value["quantities"][0]["amount"] = {
                "bool": True,
                "float": 100.0,
                "nan": float("nan"),
                "string": "100",
            }[mutation]
        raw = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    with pytest.raises(FinancialDecodeError):
        decode_financial_data(raw, NativeLiabilityVector)


@pytest.mark.parametrize(
    "payload",
    [b"", b"[]", b"null", b"\xff", b"x" * (MAX_FINANCIAL_RECOVERY_BYTES + 1)],
    ids=["empty", "list", "null", "invalid-utf8", "over-byte-cap"],
)
def test_codec_root_and_ram_bounds(payload):
    with pytest.raises(FinancialDecodeError):
        decode_financial_data(payload, FinancialOperationState)


def test_legacy_roots_and_bytes_remain_separate():
    prepared = maximum().prepared
    before = canonical_journal_bytes(prepared)
    assert decode_prepared_create(before) == prepared
    assert canonical_journal_bytes(decode_prepared_create(before)) == before
    with pytest.raises(FinancialDecodeError):
        decode_financial_data(before, PreparedCreate)
    with pytest.raises(ValueError):
        decode_admission_state(encode_financial_data(initial()))


@pytest.mark.parametrize("revision", [0, 2, True])
def test_retained_revision_requires_complete_exact_event_population(revision):
    with pytest.raises(ValueError):
        replace(reserve(), revision=revision)


def test_revocation_expiry_and_later_day_cannot_erase_old_charges():
    state = reserve()
    restored = decode_financial_data(encode_financial_data(state), FinancialOperationState)
    assert restored == state
    reg = registration()
    late = NOW + 86400
    with pytest.raises(FinancialTransitionError):
        reserve(
            state,
            intent(
                now=late, effect="topup", attempt="next", purpose=FinancialEffectPurpose.DEPOSIT
            ),
            reg,
            observed(reg, late),
            now=late,
        )
    assert financial_native_usage(state) == vector(30, 2)
    assert state.maximum == maximum()


@pytest.mark.parametrize(
    "field", ["effect_id", "attempt_id", "sign_doc_digest", "account_sequence"]
)
def test_cross_identity_duplicate_retains_exact_signing_attempt(field):
    state = confirmed()
    first = state.effects[0].intent
    later = intent(effect="topup", attempt="attempt:2", purpose=FinancialEffectPurpose.DEPOSIT)
    later = replace(later, **{field: getattr(first, field)})
    with pytest.raises((ValueError, FinancialTransitionError)):
        reserve(state, later)


def test_unknown_or_failed_create_cannot_acquire_more_funding():
    state = reserve()
    funding = intent(effect="topup", attempt="attempt:2", purpose=FinancialEffectPurpose.DEPOSIT)
    with pytest.raises(FinancialTransitionError):
        reserve(state, funding)
    failed = advance(
        advance(state, FinancialEffectState.SIGNED), FinancialEffectState.BROADCAST_CLAIMED_UNKNOWN
    )
    failed = advance(failed, FinancialEffectState.FAILED, debit=vector(0, 2), code=5)
    with pytest.raises(FinancialTransitionError):
        reserve(failed, funding)


@pytest.mark.parametrize("change", ["no_owner_revision", "future", "predates_state"])
def test_commit_data_requires_positive_owner_revision_and_causal_time(change):
    state = reserve()
    if change == "no_owner_revision":
        with pytest.raises(ValueError):
            replace(commit(state), owner_revision=0)
    else:
        ack = replace(commit(state), observed_at=NOW + 1 if change == "future" else NOW - 1)
        with pytest.raises(FinancialTransitionError):
            propose_financial_observation(
                state,
                "initial",
                observation(state.effects[0].intent, FinancialEffectState.SIGNED),
                ack,
                now=NOW,
            )


@pytest.mark.parametrize("field", ["request_digest", "sdl_digest", "source_revision"])
def test_changed_original_prepared_bytes_cannot_mutate_retained_quote(field):
    state = reserve()
    old = state.maximum.prepared
    updated = replace(old, **{field: "f" * (40 if field == "source_revision" else 64)})
    with pytest.raises(ValueError):
        replace(state, maximum=replace(state.maximum, prepared=updated))


@pytest.mark.parametrize("bad", [None, True, "active"])
def test_broadcast_claim_requires_typed_current_observation(bad):
    item = intent()
    with pytest.raises(ValueError):
        observation(item, FinancialEffectState.BROADCAST_CLAIMED_UNKNOWN, obs=bad)


def test_codec_exact_byte_budget_and_one_extra_byte(monkeypatch):
    value = initial()
    raw = encode_financial_data(value)
    monkeypatch.setattr(financial_codec, "MAX_FINANCIAL_RECOVERY_BYTES", len(raw))
    assert decode_financial_data(raw, FinancialOperationState) == value
    assert encode_financial_data(value) == raw
    with pytest.raises(FinancialDecodeError):
        decode_financial_data(raw + b" ", FinancialOperationState)
    with pytest.raises(FinancialDecodeError):
        encode_financial_data(
            replace(value, maximum=replace(value.maximum, quote_id="longer-quote-identity"))
        )


def test_codec_charges_actual_structure_nodes_on_both_encode_and_decode(monkeypatch):
    value = vector()
    raw = encode_financial_data(value)
    tree = json.loads(raw)

    def nodes(item):
        children = item.values() if type(item) is dict else item if type(item) is list else ()
        return 1 + sum(nodes(child) for child in children)

    count = nodes(tree)
    monkeypatch.setattr(financial_codec, "MAX_FINANCIAL_RECOVERY_NODES", count)
    assert encode_financial_data(value) == raw
    assert decode_financial_data(raw, NativeLiabilityVector) == value
    monkeypatch.setattr(financial_codec, "MAX_FINANCIAL_RECOVERY_NODES", count - 1)
    with pytest.raises(FinancialDecodeError):
        encode_financial_data(value)
    with pytest.raises(FinancialDecodeError):
        decode_financial_data(raw, NativeLiabilityVector)


def test_financial_decode_depth_guard_is_independent_of_byte_limit(monkeypatch):
    state = initial()
    raw = encode_financial_data(state)
    monkeypatch.setattr(financial_codec, "MAX_FINANCIAL_RECOVERY_DEPTH", 2)
    with pytest.raises(FinancialDecodeError):
        decode_financial_data(raw, FinancialOperationState)
    with pytest.raises(FinancialDecodeError):
        encode_financial_data(state)


@pytest.mark.parametrize("valid_until", [NOW + 5, NOW + 100])
def test_new_backdated_broadcast_claim_cannot_reuse_old_principal_window(valid_until):
    state = advance(reserve(), FinancialEffectState.SIGNED)
    evidence = observation(
        state.effects[0].intent,
        FinancialEffectState.BROADCAST_CLAIMED_UNKNOWN,
        now=NOW,
        obs=replace(observed(), valid_until=valid_until),
    )
    with pytest.raises(FinancialTransitionError, match="current principal"):
        propose_financial_observation(
            state, "initial", evidence, commit(state, now=NOW + 100), now=NOW + 100
        )
    assert state.effects[0].state is FinancialEffectState.SIGNED
    assert financial_native_usage(state) == vector(30, 2)


@pytest.mark.parametrize("offset", [0, 5])
def test_new_current_broadcast_claim_accepts_valid_principal_window_boundary(offset):
    state = advance(reserve(), FinancialEffectState.SIGNED)
    current = NOW + offset
    claimed = advance(
        state, FinancialEffectState.BROADCAST_CLAIMED_UNKNOWN, now=current, obs=observed()
    )
    assert claimed.effects[0].state is FinancialEffectState.BROADCAST_CLAIMED_UNKNOWN
    assert claimed.effects[0].observations[-1].observed_at == current


def test_new_broadcast_claim_refuses_expired_principal_at_actual_now():
    state = advance(reserve(), FinancialEffectState.SIGNED)
    with pytest.raises(FinancialTransitionError, match="current principal"):
        advance(state, FinancialEffectState.BROADCAST_CLAIMED_UNKNOWN, now=NOW + 6, obs=observed())


def test_fresh_new_principal_can_propose_current_claim_after_prior_window_expires():
    state = advance(reserve(), FinancialEffectState.SIGNED)
    claimed = advance(
        state,
        FinancialEffectState.BROADCAST_CLAIMED_UNKNOWN,
        now=NOW + 100,
        obs=observed(now=NOW + 100),
    )
    assert claimed.effects[0].observations[-1].principal_observation.observed_at == NOW + 100
    assert claimed.maximum == state.maximum


def test_retained_historical_broadcast_claim_recovers_without_reactivating_authority():
    retained = advance(
        advance(reserve(), FinancialEffectState.SIGNED),
        FinancialEffectState.BROADCAST_CLAIMED_UNKNOWN,
    )
    raw = encode_financial_data(retained)
    recovered = decode_financial_data(raw, FinancialOperationState)
    assert recovered == retained and encode_financial_data(recovered) == raw
    historical = recovered.effects[0].observations[-1]
    assert historical.observed_at == NOW
    assert historical.principal_observation.valid_until == NOW + 5
    with pytest.raises(FinancialTransitionError):
        propose_financial_observation(
            recovered, "initial", historical, commit(recovered, now=NOW + 100), now=NOW + 100
        )
    assert financial_native_usage(recovered) == vector(30, 2)
