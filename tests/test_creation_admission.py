from dataclasses import FrozenInstanceError, fields, replace

import pytest

from akash_lease_core import (
    CREATE_PERMIT_AUDIENCE,
    EMPTY_RESERVATION_POPULATION_DIGEST,
    AdmissionDisposition,
    AdmissionPolicyBinding,
    AdmissionReason,
    AdmissionRequest,
    AdmissionScope,
    AdmissionState,
    AuthenticatedBrokerEvidence,
    BackendIdentity,
    BackendKind,
    BoundDeployment,
    CensusStatus,
    ContainmentCensus,
    ContainmentLimits,
    CreateExposureEvidence,
    CreateOutcome,
    CreateOutcomeKind,
    CreatePermit,
    CreateSubmissionAuthorization,
    DeploymentKey,
    ExactChainReadEvidence,
    ExecutionClosure,
    ExecutionClosureProofMode,
    LifecycleIdentity,
    NonCommitEvidence,
    OutcomeReasonCode,
    OwnerBudgetScope,
    OwnerCandidate,
    OwnerEvidenceKind,
    PermitPresentation,
    PermitRedemptionError,
    PermitRedemptionProposal,
    PermitRevocationStatus,
    PersistenceConfirmation,
    PersistenceConfirmationError,
    PreparedCreate,
    PreparedGroup,
    ProducerProvenance,
    RejectionEvidence,
    ReservationProposal,
    ReservationState,
    ReservationTransitionError,
    ScopeBudget,
    SettlementEvidence,
    SettlementState,
    UncertaintyEvidence,
    canonical_admission_state_digest,
    canonical_group_population_digest,
    canonical_journal_digest,
    confirm_persisted_reservation,
    confirm_redeemed_permit,
    redeem_create_permit,
    reserve_capacity,
    transition_reservation,
)

OWNER_A = "akash1n4uut3vxmkdp8wsrya3q0qyddgqey0rh9as4ee"
OWNER_B = "akash1cklqag0e0v794msgqkr6uwf7sr6zyy42gm78f0"
NOW = 1_800_000_000
POLICY_REVISION = "9" * 40


def _scope(**changes):
    value = AdmissionScope(
        chain_id="akashnet-2",
        owner=OWNER_A,
        producer_issuer="https://token.actions.githubusercontent.com",
        repository_owner_id="456",
        repository_id="123",
        repository="example/repo",
        workload_class="ci-runner",
    )
    return replace(value, **changes)


def _prepared(operation="create:example/repo:42:1", scope=None, **changes):
    scope = scope or _scope()
    groups = (PreparedGroup(1, "example-idv1-class-ci-runner-g1-attempt-1-run-42-end"),)
    value = PreparedCreate(
        operation_id=operation,
        operation_ordinal=1,
        producer=ProducerProvenance(
            repository=scope.repository,
            repository_id=scope.repository_id,
            repository_owner_id=scope.repository_owner_id,
            issuer=scope.producer_issuer,
            audience="akash-create",
            subject="repo:example/repo:ref:refs/heads/main",
            workflow_ref="example/repo/.github/workflows/deploy.yml@refs/heads/main",
            workflow_sha="1" * 40,
            ref="refs/heads/main",
            jti="oidc-jti-1",
            authentication_evidence_digest="2" * 64,
        ),
        lifecycle=LifecycleIdentity(
            repository=scope.repository,
            workload_class=scope.workload_class,
            run=42,
            run_attempt=1,
        ),
        owner_candidate=OwnerCandidate(
            backend=BackendIdentity(BackendKind.MEDIATED, "console:production"),
            owner=scope.owner,
            evidence_kind=OwnerEvidenceKind.AUTHENTICATED_MEDIATOR,
            evidence_source="console authenticated account endpoint",
            evidence_digest="3" * 64,
        ),
        request_digest="4" * 64,
        sdl_digest="5" * 64,
        groups=groups,
        group_population_digest=canonical_group_population_digest(groups),
        source_revision="6" * 40,
        prepared_at=NOW - 2,
    )
    return replace(value, **changes)


def _exposure(prepared, **changes):
    value = CreateExposureEvidence(
        chain_id="akashnet-2",
        operation_id=prepared.operation_id,
        prepared_operation_digest=canonical_journal_digest(prepared),
        request_digest=prepared.request_digest,
        sdl_digest=prepared.sdl_digest,
        backend=prepared.owner_candidate.backend,
        backend_policy_revision=POLICY_REVISION,
        amount_uact=100,
        source="signed backend exposure policy",
        evidence_digest="7" * 64,
        observed_at=NOW - 1,
        valid_until=NOW + 20,
    )
    return replace(value, **changes)


def _request(operation="create:example/repo:42:1", scope=None, **changes):
    scope = scope or _scope()
    prepared = _prepared(operation, scope)
    value = AdmissionRequest(prepared, scope, _exposure(prepared, chain_id=scope.chain_id), NOW)
    return replace(value, **changes)


def _limits(**changes):
    value = ContainmentLimits(
        max_active_deployments=3,
        max_unresolved_create_outcomes=2,
        max_financial_exposure_uact=500,
        exposure_policy_revision=POLICY_REVISION,
        admission_policy_revision=POLICY_REVISION,
        policy_reference="policy:2026-09-11",
        valid_until=NOW + 100,
    )
    return replace(value, **changes)


def _census(**changes):
    value = ContainmentCensus(
        active_deployments=0,
        unresolved_create_outcomes=0,
        financial_exposure_uact=0,
        excluded_reservation_count=0,
        excluded_reservation_population_digest=EMPTY_RESERVATION_POPULATION_DIGEST,
        journal_status=CensusStatus.VERIFIED,
        recovery_reader_status=CensusStatus.VERIFIED,
        chain_status=CensusStatus.VERIFIED,
        accounting_status=CensusStatus.VERIFIED,
        observed_at=NOW - 10,
        valid_until=NOW + 10,
        evidence_digest="a" * 64,
    )
    return replace(value, **changes)


def _budget(scope, **changes):
    return ScopeBudget(scope, _limits(), _census(), **changes)


def _state(scope=None, *, extra=(), **changes):
    scope = scope or _scope()
    budgets = (_budget(OwnerBudgetScope(scope.chain_id, scope.owner)), _budget(scope), *extra)
    return AdmissionState(budgets, **changes)


def _proposal(state=None, request=None):
    state = state or _state()
    request = request or _request()
    result = reserve_capacity(state, request, expected_revision=state.revision, now=NOW)
    assert result.decision.disposition is AdmissionDisposition.PROPOSED
    assert result.proposal is not None
    return result.proposal


def _confirmation(proposal, **changes):
    broker = AuthenticatedBrokerEvidence(
        "firestore-broker:multi-region",
        "https://token.actions.githubusercontent.com",
        "akash-create-broker",
        "repo:example/broker:environment:production",
        "8" * 40,
        "c" * 64,
        NOW,
        NOW + 10,
    )
    value = PersistenceConfirmation(
        proposal.operation_id,
        proposal.proposed_revision,
        proposal.proposed_state_digest,
        broker,
        "b" * 64,
        NOW + 1,
    )
    return replace(value, **changes)


def _forge(value, **changes):
    forged = object.__new__(type(value))
    for field in fields(value):
        object.__setattr__(
            forged,
            field.name,
            changes.get(field.name, getattr(value, field.name)),
        )
    return forged


def _permit(request=None):
    request = request or _request()
    original = _state(request.scope)
    proposal = _proposal(original, request)
    return confirm_persisted_reservation(
        proposal,
        original,
        request,
        proposal.proposed_state,
        _confirmation(proposal),
    )


def _presentation(permit, **changes):
    value = PermitPresentation(
        permit_digest=canonical_journal_digest(permit),
        presenter_identity=permit.presenter_identity,
        audience=CREATE_PERMIT_AUDIENCE,
        current_policy_bindings=permit.policy_bindings,
        revocation_status=PermitRevocationStatus.ACTIVE,
        revocation_evidence_digest="e" * 64,
        presented_at=NOW + 1,
    )
    return replace(value, **changes)


def _submitting(request=None):
    request = request or _request()
    original = _state(request.scope)
    reservation = _proposal(original, request)
    permit = confirm_persisted_reservation(
        reservation,
        original,
        request,
        reservation.proposed_state,
        _confirmation(reservation),
    )
    redemption = redeem_create_permit(
        reservation.proposed_state,
        permit,
        _presentation(permit),
        expected_revision=reservation.proposed_revision,
    )
    confirmation = _confirmation(redemption)
    authorization = confirm_redeemed_permit(
        redemption,
        permit,
        _presentation(permit),
        reservation.proposed_state,
        redemption.proposed_state,
        confirmation,
    )
    assert isinstance(authorization, CreateSubmissionAuthorization)
    return redemption.proposed_state


def test_reserve_returns_only_a_digest_bound_proposal_without_create_authority():
    original = _state()
    result = reserve_capacity(original, _request(), expected_revision=0, now=NOW)

    assert result.decision.disposition is AdmissionDisposition.PROPOSED
    assert result.proposal.proposed_revision == 1
    assert result.proposal.expected_state_digest == canonical_admission_state_digest(original)
    assert not isinstance(result.proposal, CreatePermit)
    assert not hasattr(result.decision, "allows_new_create")
    with pytest.raises(TypeError):
        CreatePermit()
    with pytest.raises(TypeError):
        ReservationProposal()
    assert {type(item.scope) for item in result.decision.projections} == {
        OwnerBudgetScope,
        AdmissionScope,
    }


def test_only_exact_post_cas_confirmation_issues_a_permit():
    original = _state()
    request = _request()
    proposal = _proposal(original, request)
    confirmation = _confirmation(proposal)
    permit = confirm_persisted_reservation(
        proposal, original, request, proposal.proposed_state, confirmation
    )

    assert isinstance(permit, CreatePermit)
    assert permit.operation_id == proposal.operation_id
    assert permit.state_revision == proposal.proposed_revision
    assert permit.state_digest == proposal.proposed_state_digest
    assert permit.presenter_identity == request.prepared.producer.subject
    assert permit.audience == CREATE_PERMIT_AUDIENCE
    assert permit.policy_bindings == tuple(
        AdmissionPolicyBinding(item.scope, item.limits.admission_policy_revision)
        for item in proposal.proposed_state.budgets
    )


def test_confirmation_requires_current_authenticated_broker_evidence():
    original = _state()
    request = _request()
    proposal = _proposal(original, request)
    with pytest.raises(ValueError, match="authenticated broker evidence"):
        replace(_confirmation(proposal), authenticated_broker="broker-name")
    broker = _confirmation(proposal).authenticated_broker
    stale = replace(broker, verified_at=NOW - 10, valid_until=NOW)
    with pytest.raises(PersistenceConfirmationError, match="authentication is not current"):
        confirm_persisted_reservation(
            proposal,
            original,
            request,
            proposal.proposed_state,
            _confirmation(proposal, authenticated_broker=stale),
        )


def test_permit_must_be_redeemed_by_cas_before_network_authority_and_only_once():
    original = _state()
    request = _request()
    reservation = _proposal(original, request)
    permit = confirm_persisted_reservation(
        reservation,
        original,
        request,
        reservation.proposed_state,
        _confirmation(reservation),
    )
    redemption = redeem_create_permit(
        reservation.proposed_state,
        permit,
        _presentation(permit),
        expected_revision=reservation.proposed_revision,
    )
    assert redemption.proposed_state.reservations[0].state is ReservationState.SUBMITTING
    assert not isinstance(permit, CreateSubmissionAuthorization)
    with pytest.raises(TypeError):
        CreateSubmissionAuthorization()

    authorization = confirm_redeemed_permit(
        redemption,
        permit,
        _presentation(permit),
        reservation.proposed_state,
        redemption.proposed_state,
        _confirmation(redemption),
    )
    assert authorization.operation_id == permit.operation_id
    assert authorization.state_digest == redemption.proposed_state_digest
    assert authorization.presenter_identity == permit.presenter_identity
    assert authorization.audience == CREATE_PERMIT_AUDIENCE
    with pytest.raises(PermitRedemptionError, match="already redeemed"):
        redeem_create_permit(
            redemption.proposed_state,
            permit,
            _presentation(permit),
            expected_revision=redemption.proposed_revision,
        )


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ({"presenter_identity": "repo:other/repo:ref:refs/heads/main"}, "presenter"),
        ({"audience": "wrong-create-audience"}, "audience"),
        ({"revocation_status": PermitRevocationStatus.REVOKED}, "revoked"),
        ({"presented_at": NOW + 11}, "not current"),
    ],
)
def test_permit_presentation_identity_audience_revocation_and_expiry_hold(mutation, message):
    request = _request()
    original = _state(request.scope)
    reservation = _proposal(original, request)
    permit = confirm_persisted_reservation(
        reservation,
        original,
        request,
        reservation.proposed_state,
        _confirmation(reservation),
    )
    with pytest.raises(PermitRedemptionError, match=message):
        redeem_create_permit(
            reservation.proposed_state,
            permit,
            _presentation(permit, **mutation),
            expected_revision=reservation.proposed_revision,
        )


def test_policy_revision_drift_in_presentation_or_current_state_holds_redemption():
    request = _request()
    original = _state(request.scope)
    reservation = _proposal(original, request)
    permit = confirm_persisted_reservation(
        reservation,
        original,
        request,
        reservation.proposed_state,
        _confirmation(reservation),
    )
    drifted_bindings = list(permit.policy_bindings)
    drifted_bindings[0] = replace(drifted_bindings[0], policy_revision="a" * 40)
    with pytest.raises(PermitRedemptionError, match="policy revision changed"):
        redeem_create_permit(
            reservation.proposed_state,
            permit,
            _presentation(permit, current_policy_bindings=tuple(drifted_bindings)),
            expected_revision=reservation.proposed_revision,
        )

    budgets = list(reservation.proposed_state.budgets)
    budgets[0] = replace(
        budgets[0],
        limits=replace(budgets[0].limits, admission_policy_revision="a" * 40),
    )
    drifted_state = replace(reservation.proposed_state, budgets=tuple(budgets))
    with pytest.raises(PermitRedemptionError, match="policy revision changed"):
        redeem_create_permit(
            drifted_state,
            permit,
            _presentation(permit),
            expected_revision=drifted_state.revision,
        )


def test_bypassing_permit_redemption_cannot_record_any_submission_outcome():
    request = _request()
    reserved = _proposal(request=request).proposed_state
    with pytest.raises(ReservationTransitionError, match="invalid reservation transition"):
        transition_reservation(reserved, _unknown(request), expected_revision=1)


@pytest.mark.parametrize(
    "mutation",
    [
        {"operation_id": "other-operation"},
        {"persisted_revision": 2},
        {"persisted_state_digest": "c" * 64},
    ],
)
def test_mutated_persistence_confirmation_cannot_issue_permit(mutation):
    original = _state()
    request = _request()
    proposal = _proposal(original, request)
    with pytest.raises(PersistenceConfirmationError, match="disagrees"):
        confirm_persisted_reservation(
            proposal,
            original,
            request,
            proposal.proposed_state,
            _confirmation(proposal, **mutation),
        )


def test_a_different_persisted_state_cannot_issue_permit():
    original = _state()
    request = _request()
    proposal = _proposal(original, request)
    changed = replace(proposal.proposed_state, revision=2)
    with pytest.raises(PersistenceConfirmationError, match="disagrees"):
        confirm_persisted_reservation(
            proposal, original, request, changed, _confirmation(proposal)
        )


def test_forged_over_limit_reservation_proposal_cannot_issue_permit():
    original = _state()
    request = _request()
    proposal = _proposal(original, request)
    budgets = list(proposal.proposed_state.budgets)
    budgets[1] = replace(
        budgets[1],
        census=replace(budgets[1].census, active_deployments=999),
    )
    over_limit = replace(proposal.proposed_state, budgets=tuple(budgets))
    forged = _forge(
        proposal,
        proposed_state=over_limit,
        proposed_state_digest=canonical_admission_state_digest(over_limit),
    )
    with pytest.raises(PersistenceConfirmationError, match="not derived"):
        confirm_persisted_reservation(
            forged,
            original,
            request,
            over_limit,
            _confirmation(forged),
        )


def test_forged_redemption_requires_the_trusted_permit_and_pre_state():
    original = _state()
    request = _request()
    reservation = _proposal(original, request)
    permit = confirm_persisted_reservation(
        reservation,
        original,
        request,
        reservation.proposed_state,
        _confirmation(reservation),
    )
    redemption = redeem_create_permit(
        reservation.proposed_state,
        permit,
        _presentation(permit),
        expected_revision=reservation.proposed_revision,
    )
    with pytest.raises(TypeError):
        PermitRedemptionProposal()
    forged = _forge(redemption, permit_digest="d" * 64)
    with pytest.raises(PersistenceConfirmationError, match="not derived"):
        confirm_redeemed_permit(
            forged,
            permit,
            _presentation(permit),
            reservation.proposed_state,
            redemption.proposed_state,
            _confirmation(redemption),
        )


@pytest.mark.parametrize(
    "mutation",
    [
        {"operation_id": "other-operation"},
        {"prepared_operation_digest": "c" * 64},
        {"request_digest": "c" * 64},
        {"sdl_digest": "c" * 64},
        {"backend": BackendIdentity(BackendKind.MEDIATED, "console:other")},
    ],
)
def test_exposure_evidence_is_bound_to_the_exact_prepared_request(mutation):
    prepared = _prepared()
    exposure = _exposure(prepared, **mutation)
    with pytest.raises(ValueError, match="exposure evidence disagrees"):
        AdmissionRequest(prepared, _scope(), exposure, NOW)


def test_exposure_policy_revision_must_match_owner_and_leaf_policy():
    state = _state()
    request = _request()
    request = replace(
        request,
        exposure=replace(request.exposure, backend_policy_revision="8" * 40),
    )
    result = reserve_capacity(state, request, expected_revision=0, now=NOW)
    assert result.decision.reason is AdmissionReason.EXPOSURE_POLICY_MISMATCH
    assert result.proposal is None


@pytest.mark.parametrize("budget_index", [0, 1])
@pytest.mark.parametrize(
    ("limit_change", "reason"),
    [
        ({"max_active_deployments": 0}, AdmissionReason.ACTIVE_DEPLOYMENT_LIMIT),
        ({"max_unresolved_create_outcomes": 0}, AdmissionReason.UNRESOLVED_OUTCOME_LIMIT),
        ({"max_financial_exposure_uact": 99}, AdmissionReason.FINANCIAL_EXPOSURE_LIMIT),
    ],
)
def test_each_owner_or_leaf_ceiling_can_independently_block_atomic_proposal(
    budget_index, limit_change, reason
):
    state = _state()
    budgets = list(state.budgets)
    budgets[budget_index] = replace(budgets[budget_index], limits=_limits(**limit_change))
    result = reserve_capacity(
        replace(state, budgets=tuple(budgets)), _request(), expected_revision=0, now=NOW
    )
    assert result.decision.reason is reason
    assert result.decision.failed_scope == budgets[budget_index].scope
    assert result.proposal is None


def test_missing_owner_or_leaf_budget_holds():
    for only in ((_budget(OwnerBudgetScope("akashnet-2", OWNER_A)),), (_budget(_scope()),)):
        result = reserve_capacity(AdmissionState(only), _request(), expected_revision=0, now=NOW)
        assert result.decision.reason is AdmissionReason.MISSING_REQUIRED_BUDGET


def test_stale_concurrent_writer_cannot_propose_against_a_new_revision():
    current = _proposal().proposed_state
    result = reserve_capacity(
        current,
        _request("operation-2"),
        expected_revision=0,
        now=NOW,
    )
    assert result.decision.reason is AdmissionReason.STALE_REVISION
    assert result.proposal is None


@pytest.mark.parametrize(
    ("field", "reason"),
    [
        ("journal_status", AdmissionReason.JOURNAL_UNHEALTHY),
        ("recovery_reader_status", AdmissionReason.RECOVERY_READER_UNHEALTHY),
        ("chain_status", AdmissionReason.CHAIN_CENSUS_UNHEALTHY),
        ("accounting_status", AdmissionReason.ACCOUNTING_CENSUS_UNHEALTHY),
    ],
)
def test_each_incomplete_census_instrument_holds(field, reason):
    state = _state()
    budgets = list(state.budgets)
    budgets[1] = replace(budgets[1], census=_census(**{field: CensusStatus.INCOMPLETE}))
    result = reserve_capacity(
        replace(state, budgets=tuple(budgets)), _request(), expected_revision=0, now=NOW
    )
    assert result.decision.reason is reason
    assert result.proposal is None


@pytest.mark.parametrize(
    "mutation",
    [
        {"excluded_reservation_count": 1},
        {"excluded_reservation_population_digest": "d" * 64},
    ],
)
def test_reservation_population_mismatch_holds_before_any_projection(mutation):
    state = _state()
    budgets = list(state.budgets)
    budgets[0] = replace(budgets[0], census=_census(**mutation))
    result = reserve_capacity(
        replace(state, budgets=tuple(budgets)), _request(), expected_revision=0, now=NOW
    )
    assert result.decision.reason is AdmissionReason.RESERVATION_CENSUS_MISMATCH
    assert result.decision.projections == ()


def test_stale_exposure_evidence_holds():
    request = _request()
    request = replace(
        request,
        exposure=replace(request.exposure, valid_until=NOW - 1),
    )
    result = reserve_capacity(_state(), request, expected_revision=0, now=NOW)
    assert result.decision.reason is AdmissionReason.EXPOSURE_EVIDENCE_NOT_CURRENT


def test_owner_aggregate_counts_two_leaf_scopes_but_leaves_remain_isolated():
    scope_a = _scope()
    scope_b = _scope(repository="example/other", repository_id="124")
    initial = _state(scope_a, extra=(_budget(scope_b),))
    first = _proposal(initial, _request("operation-a", scope_a)).proposed_state
    second = _proposal(first, _request("operation-b", scope_b)).proposed_state

    owner = next(item for item in second.budgets if isinstance(item.scope, OwnerBudgetScope))
    leaf_a = next(item for item in second.budgets if item.scope == scope_a)
    leaf_b = next(item for item in second.budgets if item.scope == scope_b)
    assert owner.census.excluded_reservation_count == 2
    assert leaf_a.census.excluded_reservation_count == 1
    assert leaf_b.census.excluded_reservation_count == 1


def test_a_full_owner_does_not_block_an_unrelated_owner():
    scope_b = _scope(owner=OWNER_B, repository="example/other", repository_id="124")
    full_a = replace(
        _budget(OwnerBudgetScope("akashnet-2", OWNER_A)),
        census=_census(active_deployments=3),
    )
    state = AdmissionState(
        (
            full_a,
            _budget(_scope()),
            _budget(OwnerBudgetScope("akashnet-2", OWNER_B)),
            _budget(scope_b),
        )
    )
    assert (
        reserve_capacity(
            state, _request("operation-b", scope_b), expected_revision=0, now=NOW
        ).decision.disposition
        is AdmissionDisposition.PROPOSED
    )


def test_owner_aggregate_isolated_by_chain_id_for_the_same_signer():
    chain_a = _scope(chain_id="akashnet-a")
    chain_b = _scope(chain_id="akashnet-b")
    full_a = replace(
        _budget(OwnerBudgetScope(chain_a.chain_id, chain_a.owner)),
        census=_census(active_deployments=3),
    )
    state = AdmissionState(
        (
            full_a,
            _budget(chain_a),
            _budget(OwnerBudgetScope(chain_b.chain_id, chain_b.owner)),
            _budget(chain_b),
        )
    )
    result = reserve_capacity(
        state,
        _request("operation-chain-b", chain_b),
        expected_revision=0,
        now=NOW,
    )
    assert result.decision.disposition is AdmissionDisposition.PROPOSED
    assert {projection.scope for projection in result.decision.projections} == {
        OwnerBudgetScope(chain_b.chain_id, chain_b.owner),
        chain_b,
    }


def test_existing_and_terminal_operation_replays_are_reconcile_only():
    request = _request()
    state = _proposal(request=request).proposed_state
    replay = reserve_capacity(state, request, expected_revision=0, now=NOW)
    assert replay.decision.disposition is AdmissionDisposition.RECONCILE
    assert replay.decision.reason is AdmissionReason.RECONCILE_EXISTING
    assert replay.proposal is None

    submitting = _submitting(request)
    terminal = transition_reservation(
        submitting, _rejected(request), expected_revision=submitting.revision
    )
    replay = reserve_capacity(terminal, request, expected_revision=0, now=NOW)
    assert replay.decision.disposition is AdmissionDisposition.RECONCILE
    assert replay.proposal is None


def test_changed_same_operation_is_a_conflict_not_reconcile():
    request = _request()
    state = _proposal(request=request).proposed_state
    changed = replace(
        request,
        exposure=replace(request.exposure, amount_uact=101, evidence_digest="c" * 64),
    )
    result = reserve_capacity(state, changed, expected_revision=state.revision, now=NOW)
    assert result.decision.reason is AdmissionReason.OPERATION_CONFLICT


def _rejected(request):
    evidence = RejectionEvidence(
        request.operation_id,
        request.prepared.owner_candidate.backend,
        True,
        "backend pre-send rejection",
        "c" * 64,
        NOW + 2,
    )
    return CreateOutcome(
        request.operation_id,
        CreateOutcomeKind.REJECTED_BEFORE_SEND,
        OutcomeReasonCode.INTENT_REJECTED,
        rejection=evidence,
    )


def _fallback_safe(request, **changes):
    evidence = NonCommitEvidence(
        request.operation_id,
        request.prepared.owner_candidate.backend,
        "submission-1",
        None,
        request.scope.owner,
        request.prepared.group_population_digest,
        "rpc-a",
        "rpc-b",
        "d" * 64,
        NOW + 2,
    )
    evidence = replace(evidence, **changes)
    return CreateOutcome(
        request.operation_id,
        CreateOutcomeKind.FALLBACK_SAFE,
        OutcomeReasonCode.SUBMISSION_PROVEN_ABSENT,
        non_commit=evidence,
    )


def _unknown(request):
    return CreateOutcome(
        request.operation_id,
        CreateOutcomeKind.UNKNOWN,
        OutcomeReasonCode.HTTP_READ_TIMEOUT,
        uncertainty=UncertaintyEvidence(request.operation_id, "console", "e" * 64, NOW + 2),
    )


def _committed(request):
    subject = DeploymentKey(request.scope.owner, "9001")
    read = ExactChainReadEvidence(
        request.operation_id,
        subject,
        "akashnet-2",
        123,
        request.prepared.groups,
        request.prepared.group_population_digest,
        "rpc-a",
        "f" * 64,
        NOW + 2,
    )
    deployment = BoundDeployment(
        request.operation_id,
        subject,
        request.prepared.groups,
        request.prepared.group_population_digest,
        read,
    )
    return CreateOutcome(
        request.operation_id,
        CreateOutcomeKind.COMMITTED,
        OutcomeReasonCode.EXACT_CHAIN_READ_BOUND,
        deployment=deployment,
    )


def _closure(request, subject=None):
    return ExecutionClosure(
        request.operation_id,
        subject or DeploymentKey(request.scope.owner, "9001"),
        "akashnet-2",
        ExecutionClosureProofMode.EXACT_FINALIZED_HEIGHT,
        "rpc-a",
        "rpc-b",
        "operator-a",
        "operator-b",
        "trust-a",
        "trust-b",
        True,
        125,
        125,
        125,
        124,
        request.prepared.group_population_digest,
        "1" * 64,
        "2" * 64,
        NOW + 3,
    )


def _settlement(request, state=SettlementState.SETTLED, subject=None):
    return SettlementEvidence(
        request.operation_id,
        subject or DeploymentKey(request.scope.owner, "9001"),
        state,
        "rpc-a",
        "rpc-b",
        "3" * 64,
        NOW + 4,
    )


def test_typed_outcomes_release_only_their_own_populations():
    request = _request()
    submitting = _submitting(request)
    unknown = transition_reservation(submitting, _unknown(request), expected_revision=2)
    held = unknown.reservations[0]
    assert (held.active_slots, held.unresolved_slots, held.financial_exposure_uact) == (
        1,
        1,
        100,
    )

    committed = transition_reservation(submitting, _committed(request), expected_revision=2)
    held = committed.reservations[0]
    assert (held.active_slots, held.unresolved_slots, held.financial_exposure_uact) == (
        1,
        0,
        100,
    )
    closed = transition_reservation(committed, _closure(request), expected_revision=3)
    held = closed.reservations[0]
    assert (held.active_slots, held.unresolved_slots, held.financial_exposure_uact) == (
        0,
        0,
        100,
    )
    settled = transition_reservation(closed, _settlement(request), expected_revision=4)
    assert settled.reservations[0].financial_exposure_uact == 0

    noncommit = transition_reservation(submitting, _fallback_safe(request), expected_revision=2)
    held = noncommit.reservations[0]
    assert (held.active_slots, held.unresolved_slots, held.financial_exposure_uact) == (
        0,
        0,
        0,
    )


def test_arbitrary_digest_or_untyped_release_cannot_advance_state():
    state = _proposal().proposed_state
    with pytest.raises((TypeError, ValueError)):
        transition_reservation(
            state,
            "f" * 64,
            expected_revision=1,
        )


def test_foreign_noncommit_owner_cannot_release_capacity():
    request = _request()
    state = _submitting(request)
    with pytest.raises(ReservationTransitionError, match="disagrees"):
        transition_reservation(
            state,
            _fallback_safe(request, exact_owner=OWNER_B),
            expected_revision=2,
        )


def test_committed_group_population_must_match_the_reserved_prepared_operation():
    request = _request()
    state = _submitting(request)
    groups = (PreparedGroup(1, "different-group"),)
    digest = canonical_group_population_digest(groups)
    subject = DeploymentKey(request.scope.owner, "9001")
    read = ExactChainReadEvidence(
        request.operation_id,
        subject,
        "akashnet-2",
        123,
        groups,
        digest,
        "rpc-a",
        "4" * 64,
        NOW + 2,
    )
    outcome = CreateOutcome(
        request.operation_id,
        CreateOutcomeKind.COMMITTED,
        OutcomeReasonCode.EXACT_CHAIN_READ_BOUND,
        deployment=BoundDeployment(request.operation_id, subject, groups, digest, read),
    )
    with pytest.raises(ReservationTransitionError, match="disagrees"):
        transition_reservation(state, outcome, expected_revision=2)


def test_foreign_closure_subject_and_unsettled_finance_cannot_release():
    request = _request()
    state = _submitting(request)
    committed = transition_reservation(state, _committed(request), expected_revision=2)
    with pytest.raises(ReservationTransitionError, match="disagrees"):
        transition_reservation(
            committed,
            _closure(request, DeploymentKey(OWNER_B, "9001")),
            expected_revision=2,
        )
    closed = transition_reservation(committed, _closure(request), expected_revision=3)
    with pytest.raises(ReservationTransitionError, match="measured settlement"):
        transition_reservation(
            closed,
            _settlement(request, SettlementState.UNSETTLED),
            expected_revision=4,
        )


def test_state_and_policy_values_are_immutable():
    with pytest.raises(FrozenInstanceError):
        _limits().max_active_deployments = 100
