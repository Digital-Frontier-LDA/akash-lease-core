from __future__ import annotations

import ast
import inspect
from dataclasses import replace

import pytest

import akash_lease_core.close_policy as policy_module
from akash_lease_core import (
    CLOSE_POLICY_VERSION,
    EMPTY_POPULATION_DIGEST,
    AttestationVerification,
    BypassUseStatus,
    CandidateCompleteness,
    CandidatePopulation,
    ChainEvidence,
    ChainProofMode,
    CIAuthority,
    CloseDecision,
    CloseDisposition,
    CloseIntent,
    CloseReasonCode,
    ConsumerState,
    CreationAttestation,
    CreationBindingMode,
    CreationBindingStatus,
    CreatorRollbackAuthority,
    DeploymentKey,
    EnvironmentControlStatus,
    GroupObservation,
    HandoffState,
    Identity,
    IsolatedSignerEvidence,
    LeasePopulationCompleteness,
    MembershipStatus,
    PopulationCompleteness,
    PreCloseDeploymentState,
    PrincipalKind,
    ProductionApprovalMode,
    ProductionAuthorizationAction,
    ProductionBypassEvidence,
    ProductionPrincipal,
    ProductionRetirementAuthority,
    RollbackTriggerEvidence,
    RunState,
    SharedSignerEvidence,
    SourceAgreement,
    StagingRetirementAuthority,
    UniquenessStatus,
    VerificationStatus,
    canonical_attestation_digest,
    canonical_deployment_population_digest,
    canonical_group_identity_digest,
    canonical_prepared_operation_key,
    classify_groups,
    evaluate_close,
    format_identity,
)

OWNER = "akash1n4uut3vxmkdp8wsrya3q0qyddgqey0rh9as4ee"
OTHER_OWNER = "akash1cklqag0e0v794msgqkr6uwf7sr6zyy42gm78f0"
SUBJECT = DeploymentKey(OWNER, "1787378468067")
OTHER_SUBJECT = DeploymentKey(OTHER_OWNER, "1787378468068")
REGISTER = {"example": "example-org/example-repo"}
NOW = 1_757_500_000


def _population(workload_class: str):
    fields = dict(
        prefix="example",
        owner=REGISTER["example"],
        workload_class=workload_class,
        group=1,
    )
    if workload_class.startswith("ci-"):
        fields.update(run=12345, attempt=2)
    else:
        fields.update(release="release_7")
        if workload_class == "staging-payload":
            fields.update(expires=2_000_000_000)
    first = Identity(**fields)
    identities = (first, replace(first, group=2))
    groups = [GroupObservation(item.group, format_identity(item, REGISTER)) for item in identities]
    return classify_groups(groups, REGISTER, completeness=PopulationCompleteness.COMPLETE)


def _candidates(population=None, **updates):
    population = population or _population("ci-runner")
    identity = population.identities[0]
    group_digest = canonical_group_identity_digest(population)
    operation_id = "create-op-123"
    values = dict(
        subject=SUBJECT,
        group_identity_digest=group_digest,
        operation_id=operation_id,
        operation_ordinal=1,
        count=1,
        completeness=CandidateCompleteness.COMPLETE,
        prepared_operation_key=canonical_prepared_operation_key(
            signer_owner=SUBJECT.owner,
            producer_repository=identity.owner,
            workload_class=identity.workload_class,
            operation_id=operation_id,
            operation_ordinal=1,
            prepared_group_identity_digest=group_digest,
            run=identity.run,
            run_attempt=identity.attempt,
            release=identity.release,
        ),
        uniqueness_status=UniquenessStatus.UNIQUE,
        evidence_digest="candidate-journal-digest",
        creation_authorization_reference=("release-approval-9" if identity.release else None),
    )
    values.update(updates)
    if "prepared_operation_key" not in updates:
        values["prepared_operation_key"] = canonical_prepared_operation_key(
            signer_owner=SUBJECT.owner,
            producer_repository=identity.owner,
            workload_class=identity.workload_class,
            operation_id=values["operation_id"],
            operation_ordinal=values["operation_ordinal"],
            prepared_group_identity_digest=values["group_identity_digest"],
            run=identity.run,
            run_attempt=identity.attempt,
            release=identity.release,
        )
    return CandidatePopulation(**values)


def _chain(population, **updates):
    values = dict(
        subject=SUBJECT,
        group_identity_digest=canonical_group_identity_digest(population),
        evidence_digest="two-source-preclose-digest",
        chain_id="akashnet-2",
        source_a="rpc-a.example",
        source_b="rpc-b.example",
        trust_path_a="operator-a/root-1",
        trust_path_b="operator-b/root-2",
        source_a_height=20_000_000,
        source_b_height=20_000_000,
        common_finality_height=20_000_000,
        proof_mode=ChainProofMode.EXACT_FINALIZED_HEIGHT,
        deployment_count=1,
        deployment_population_digest=canonical_deployment_population_digest(SUBJECT),
        deployment_state=PreCloseDeploymentState.ACTIVE,
        deployment_state_digest="active-deployment-state-digest",
        group_count=population.observed_count,
        group_population_digest=canonical_group_identity_digest(population),
        group_state_digest="complete-group-state-digest",
        lease_count=2,
        lease_population_digest="two-complete-leases-digest",
        lease_state_digest="complete-lease-state-digest",
        lease_completeness=LeasePopulationCompleteness.COMPLETE,
        observed_at=NOW,
        valid_until=NOW + 60,
        agreement=SourceAgreement.AGREEING,
    )
    values.update(updates)
    return ChainEvidence(**values)


def _ci_authority(**updates):
    values = dict(
        subject=SUBJECT,
        repository=REGISTER["example"],
        run=12345,
        attempt=2,
        run_state=RunState.TERMINAL,
        consumer_state=ConsumerState.FINISHED,
        source="github-api:example/repo",
        observation_digest="github-run-and-consumers-digest",
        observed_at=NOW,
        valid_until=NOW + 60,
    )
    values.update(updates)
    return CIAuthority(**values)


def _isolated(workload_class="ci-runner", **updates):
    values = dict(
        signer_owner=SUBJECT.owner,
        repository=REGISTER["example"],
        workload_class=workload_class,
        operation_id="create-op-123",
        environment="production"
        if workload_class == "prod-payload"
        else ("staging" if workload_class == "staging-payload" else None),
        trust_domain=f"example/repo:{workload_class}-signer",
        source="backend-owner-read",
        evidence_digest="backend-owner-evidence-digest",
        observed_at=NOW,
        valid_until=NOW + 60,
    )
    values.update(updates)
    return IsolatedSignerEvidence(**values)


def _claim(population, **updates):
    identity = population.identities[0]
    values = dict(
        schema_version=1,
        audience="akash-deployment-create",
        issuer="https://trusted-deployment-broker.example",
        operation_id="create-op-123",
        subject=SUBJECT,
        group_identity_digest=canonical_group_identity_digest(population),
        producer_repository=identity.owner,
        workload_class=identity.workload_class,
        repository_id="123456789",
        repository_owner_id="987654321",
        ref="refs/heads/main",
        workflow_ref="example/repo/.github/workflows/deploy.yml@refs/heads/main",
        workflow_sha="b" * 40,
        producer_oidc_issuer="https://token.actions.githubusercontent.com",
        producer_oidc_audience="akash-deployment-broker",
        producer_oidc_subject="repo:example/repo:ref:refs/heads/main",
        producer_oidc_jti="producer-token-jti-123",
        job_workflow_ref="df-cicd/.github/workflows/akash.yml@refs/heads/main",
        job_workflow_sha="c" * 40,
        issued_at=NOW - 30,
        run=identity.run,
        run_attempt=identity.attempt,
        release=identity.release,
        authorization_reference="release-approval-9" if identity.release else None,
        environment="production"
        if identity.workload_class == "prod-payload"
        else ("staging" if identity.workload_class == "staging-payload" else None),
    )
    values.update(updates)
    return CreationAttestation(**values)


def _shared(population, claim_updates=None, verification_updates=None):
    claim = _claim(population, **(claim_updates or {}))
    values = dict(
        attestation_digest=canonical_attestation_digest(claim),
        trust_root="sha256:pinned-broker-key",
        verified_issuer=claim.issuer,
        verified_audience=claim.audience,
        verified_producer_oidc_issuer=claim.producer_oidc_issuer,
        verified_producer_oidc_audience=claim.producer_oidc_audience,
        verified_producer_oidc_subject=claim.producer_oidc_subject,
        verified_producer_oidc_jti=claim.producer_oidc_jti,
        signature_status=VerificationStatus.VERIFIED,
        uniqueness_status=UniquenessStatus.UNIQUE,
        producer_jti_uniqueness_status=UniquenessStatus.UNIQUE,
        observed_at=NOW,
        valid_until=NOW + 60,
    )
    values.update(verification_updates or {})
    return SharedSignerEvidence(claim, AttestationVerification(**values))


def _principal(principal, kind, **updates):
    values = dict(
        principal=principal,
        kind=kind,
        authentication_status=VerificationStatus.VERIFIED,
        admin_membership=MembershipStatus.VERIFIED_NON_MEMBER,
        approval_role_membership=MembershipStatus.VERIFIED_NON_MEMBER,
        evidence_source="github-principal-and-role-api",
        evidence_digest=f"principal-evidence:{principal}",
    )
    values.update(updates)
    return ProductionPrincipal(**values)


def _payload_authority(kind, **updates):
    if kind == "prod":
        cls = ProductionRetirementAuthority
        values = dict(
            subject=SUBJECT,
            repository=REGISTER["example"],
            release="release_7",
            environment="production",
            authorization_id="prod-auth-123",
            authorization_reference="prod-retirement-2",
            requester=_principal("bot:requester", PrincipalKind.MACHINE),
            approver=_principal(
                "user:approver",
                PrincipalKind.HUMAN,
                approval_role_membership=MembershipStatus.VERIFIED_MEMBER,
            ),
            executor=_principal("service:executor", PrincipalKind.MACHINE),
            action=ProductionAuthorizationAction.PRODUCTION_RETIREMENT,
            approval_mode=ProductionApprovalMode.GITHUB_ENVIRONMENT,
            approval_verification_status=VerificationStatus.VERIFIED,
            single_use_verification=VerificationStatus.VERIFIED,
            authorization_uniqueness_status=UniquenessStatus.UNIQUE,
            prevent_self_review_status=EnvironmentControlStatus.VERIFIED_ENABLED,
            bypass=ProductionBypassEvidence(
                configuration_status=EnvironmentControlStatus.VERIFIED_DISABLED,
                use_status=BypassUseStatus.UNUSED,
                observation_verification_status=VerificationStatus.VERIFIED,
                enabled_bypass_policy_status=VerificationStatus.UNKNOWN,
                observation_source="github-environment-deployment-event",
                observation_digest="bypass-observation-digest",
                actor_principal=None,
            ),
            policy_reference="github-environment:production",
            approval_verification_source="github-environment-api",
            approval_verification_digest="production-approval-digest",
            verified_authorization_id="prod-auth-123",
            verified_environment="production",
            verified_release="release_7",
            verified_subject=SUBJECT,
            verified_action=ProductionAuthorizationAction.PRODUCTION_RETIREMENT,
            single_use_authorization_id="prod-auth-123",
            unique_authorization_id="prod-auth-123",
            issued_at=NOW - 30,
            observed_at=NOW,
            valid_until=NOW + 60,
            workflow_ref="example/repo/.github/workflows/prod.yml@refs/heads/main",
            workflow_sha="d" * 40,
            ref="refs/heads/main",
            verified_workflow_ref=("example/repo/.github/workflows/prod.yml@refs/heads/main"),
            verified_workflow_sha="d" * 40,
            verified_ref="refs/heads/main",
        )
    else:
        cls = StagingRetirementAuthority
        values = dict(
            subject=SUBJECT,
            repository=REGISTER["example"],
            release="release_7",
            authorization_reference="staging-retirement-2",
            source="staging-release-policy",
            observation_digest="staging-release-policy-digest",
            observed_at=NOW,
            valid_until=NOW + 60,
        )
    values.update(updates)
    return cls(**values)


def _rollback(population, **updates):
    identity = population.identities[0]
    values = dict(
        subject=SUBJECT,
        operation_id="create-op-123",
        repository=identity.owner,
        workload_class=identity.workload_class,
        run=identity.run,
        run_attempt=identity.attempt,
        release=identity.release,
        expires=identity.expires,
        creation_authorization_reference=("release-approval-9" if identity.release else None),
        backend="backend-slot-1",
        signer_owner=SUBJECT.owner,
        group_identity_digest=canonical_group_identity_digest(population),
        capability_reference="prepared-record:create-op-123",
        binding_status=CreationBindingStatus.CONFIRMED,
        binding_mode=CreationBindingMode.TRANSACTION_EVENT,
        binding_source="create-tx-event:ABC123",
        binding_evidence_digest="create-tx-event-digest",
        prepared_at=NOW - 30,
        valid_until=NOW + 60,
        rollback_trigger=RollbackTriggerEvidence(
            handoff_state=HandoffState.FAILED,
            source="handoff-journal:create-op-123",
            evidence_digest="failed-handoff-digest",
            observed_at=NOW,
            valid_until=NOW + 60,
        ),
    )
    values.update(updates)
    return CreatorRollbackAuthority(**values)


def _evaluate(population, intent, authority, **updates):
    group_digest = canonical_group_identity_digest(population) if not population.held else "held"
    values = dict(
        subject=SUBJECT,
        population=population,
        candidates=_candidates(
            population if population.identities and not population.held else None,
            group_identity_digest=group_digest,
        ),
        intent=intent,
        authority=authority,
        chain_evidence=_chain(population) if not population.held else None,
        producer_authentication=(
            _isolated(population.identities[0].workload_class) if population.identities else None
        ),
        evaluated_at=NOW,
    )
    values.update(updates)
    return evaluate_close(**values)


def test_ci_allow_returns_subject_population_authority_and_evidence_bindings():
    population = _population("ci-runner")
    decision = _evaluate(population, CloseIntent.CI_CLEANUP, _ci_authority())
    assert decision.allowed
    assert decision.workload_class == "ci-runner"
    assert (decision.observed_groups, decision.matching_candidate_count) == (2, 1)
    assert decision.group_identity_digest == canonical_group_identity_digest(population)
    assert decision.authority_principal == f"ci:{REGISTER['example']}:12345:2"
    assert decision.authority_digest
    assert decision.preclose_evidence_digest == "two-source-preclose-digest"
    assert (decision.evidence_observed_at, decision.evidence_valid_until) == (NOW, NOW + 60)
    assert decision.evaluated_at == NOW
    assert decision.operation_id == "create-op-123"
    assert decision.candidate_evidence_digest == "candidate-journal-digest"
    assert decision.prepared_operation_key == _candidates().prepared_operation_key
    assert decision.chain_id == "akashnet-2"
    assert decision.proof_mode is ChainProofMode.EXACT_FINALIZED_HEIGHT
    assert decision.finalized_height == 20_000_000


@pytest.mark.parametrize("workload_class", ["staging-payload", "prod-payload"])
def test_ci_denies_staging_and_production_before_missing_evidence(workload_class):
    decision = _evaluate(
        _population(workload_class),
        CloseIntent.CI_CLEANUP,
        None,
        chain_evidence=None,
        producer_authentication=None,
    )
    assert decision.disposition is CloseDisposition.DENY


@pytest.mark.parametrize(
    "mutation",
    [
        {"run": 999},
        {"attempt": 3},
        {"attempt": True},
        {"run_state": RunState.LIVE},
        {"run_state": RunState.UNKNOWN},
        {"consumer_state": ConsumerState.ACTIVE},
        {"consumer_state": ConsumerState.UNKNOWN},
        {"subject": OTHER_SUBJECT},
        {"source": ""},
        {"observation_digest": ""},
        {"valid_until": NOW - 1},
    ],
)
def test_ci_authority_effect_mutations_refuse(mutation):
    decision = _evaluate(
        _population("ci-payload"), CloseIntent.CI_CLEANUP, _ci_authority(**mutation)
    )
    evidence_gap = set(mutation) <= {"source", "observation_digest", "valid_until"}
    expected = CloseDisposition.HOLD if evidence_gap else CloseDisposition.DENY
    assert decision.disposition is expected


def test_missing_or_stale_authority_holds():
    population = _population("ci-runner")
    missing = _evaluate(population, CloseIntent.CI_CLEANUP, None)
    stale = _evaluate(
        population,
        CloseIntent.CI_CLEANUP,
        _ci_authority(valid_until=NOW - 1),
    )
    assert missing.disposition is stale.disposition is CloseDisposition.HOLD


@pytest.mark.parametrize(
    "mutation",
    [
        {"count": 0},
        {"count": 2},
        {"completeness": CandidateCompleteness.INCOMPLETE},
        {"uniqueness_status": UniquenessStatus.REUSED},
        {"uniqueness_status": UniquenessStatus.UNKNOWN},
        {"prepared_operation_key": ""},
        {"evidence_digest": ""},
        {"subject": OTHER_SUBJECT},
        {"group_identity_digest": "0" * 64},
        {"operation_id": "another-operation"},
        {
            "operation_ordinal": 2,
            "prepared_operation_key": _candidates().prepared_operation_key,
        },
    ],
)
def test_candidate_population_effect_mutations_hold(mutation):
    decision = _evaluate(
        _population("ci-runner"),
        CloseIntent.CI_CLEANUP,
        _ci_authority(),
        candidates=_candidates(**mutation),
    )
    assert decision.disposition is CloseDisposition.HOLD


def test_candidate_creation_authorization_matches_lifecycle():
    ci = _population("ci-runner")
    payload = _population("prod-payload")
    assert (
        _evaluate(
            ci,
            CloseIntent.CI_CLEANUP,
            _ci_authority(),
            candidates=_candidates(ci, creation_authorization_reference="unexpected"),
        ).disposition
        is CloseDisposition.HOLD
    )
    assert (
        _evaluate(
            payload,
            CloseIntent.PRODUCTION_RETIREMENT,
            _payload_authority("prod"),
            candidates=_candidates(payload, creation_authorization_reference=None),
        ).disposition
        is CloseDisposition.HOLD
    )


@pytest.mark.parametrize(
    "mutation",
    [
        {"subject": OTHER_SUBJECT},
        {"group_identity_digest": "0" * 64},
        {"evidence_digest": ""},
        {"source_b": "rpc-a.example"},
        {"trust_path_b": "operator-a/root-1"},
        {"common_finality_height": 20_000_011},
        {"proof_mode": ChainProofMode.LATEST_UNFINALIZED},
        {"valid_until": NOW - 1},
        {"observed_at": NOW + 1},
        {"deployment_state": PreCloseDeploymentState.UNKNOWN},
        {"deployment_count": 0},
        {"deployment_population_digest": "0" * 64},
        {"deployment_state_digest": ""},
        {"group_count": 0},
        {"group_count": 1},
        {"group_population_digest": "0" * 64},
        {"group_state_digest": ""},
        {"lease_count": 0},
        {"lease_population_digest": EMPTY_POPULATION_DIGEST},
        {"lease_state_digest": ""},
        {"lease_completeness": LeasePopulationCompleteness.INCOMPLETE},
        {"agreement": SourceAgreement.DISAGREEING},
        {"agreement": SourceAgreement.INCOMPLETE},
    ],
)
def test_preclose_chain_effect_mutations_hold(mutation):
    population = _population("ci-runner")
    decision = _evaluate(
        population,
        CloseIntent.CI_CLEANUP,
        _ci_authority(),
        chain_evidence=_chain(population, **mutation),
    )
    assert not decision.allowed


def test_exact_complete_never_leased_population_is_nonvacuous_and_allowed():
    population = _population("ci-runner")
    evidence = _chain(
        population,
        lease_count=0,
        lease_population_digest=EMPTY_POPULATION_DIGEST,
        lease_completeness=LeasePopulationCompleteness.EXACT_NEVER_LEASED,
    )

    decision = _evaluate(
        population,
        CloseIntent.CI_CLEANUP,
        _ci_authority(),
        chain_evidence=evidence,
    )

    assert decision.allowed


def test_incomplete_mixed_and_unknown_groups_hold():
    complete = _population("ci-runner")
    populations = [
        replace(complete, completeness=PopulationCompleteness.INCOMPLETE, held=True),
        replace(
            complete,
            identities=(complete.identities[0], _population("prod-payload").identities[1]),
            held=True,
            reason="mixed identities",
        ),
        classify_groups(
            [GroupObservation(1, "legacy-name")],
            REGISTER,
            completeness=PopulationCompleteness.COMPLETE,
        ),
    ]
    for population in populations:
        assert (
            _evaluate(population, CloseIntent.CI_CLEANUP, None).disposition
            is CloseDisposition.HOLD
        )


def test_staging_and_production_require_distinct_authorities():
    staging = _population("staging-payload")
    production = _population("prod-payload")
    assert _evaluate(
        staging, CloseIntent.STAGING_RETIREMENT, _payload_authority("staging")
    ).allowed
    assert _evaluate(
        production, CloseIntent.PRODUCTION_RETIREMENT, _payload_authority("prod")
    ).allowed
    assert (
        _evaluate(
            production, CloseIntent.PRODUCTION_RETIREMENT, _payload_authority("staging")
        ).disposition
        is CloseDisposition.DENY
    )


@pytest.mark.parametrize(
    "mutation",
    [
        {"authorization_uniqueness_status": UniquenessStatus.REUSED},
        {"single_use_verification": VerificationStatus.FAILED},
        {"prevent_self_review_status": EnvironmentControlStatus.UNKNOWN},
        {"action": ProductionAuthorizationAction.OTHER},
        {"environment": "staging"},
        {"workflow_ref": ""},
        {"workflow_ref": "example/repo/.github/workflows/wrong.yml@refs/heads/main"},
        {"workflow_sha": "refs/heads/main"},
        {"ref": ""},
        {"ref": "refs/heads/wrong"},
        {"release": "other_release"},
        {"subject": OTHER_SUBJECT},
        {"approval_verification_status": VerificationStatus.FAILED},
        {"authorization_id": ""},
        {"authorization_id": "other-auth"},
        {"verified_environment": "staging"},
        {"verified_action": ProductionAuthorizationAction.OTHER},
    ],
)
def test_production_authority_effect_mutations_refuse(mutation):
    population = _population("prod-payload")
    decision = _evaluate(
        population,
        CloseIntent.PRODUCTION_RETIREMENT,
        _payload_authority("prod", **mutation),
    )
    assert not decision.allowed


def test_single_operator_and_automated_request_production_shapes_allow():
    population = _population("prod-payload")
    automated = _payload_authority("prod")
    automated_without_self_review_guard = _payload_authority(
        "prod",
        prevent_self_review_status=EnvironmentControlStatus.VERIFIED_DISABLED,
    )
    operator = _principal(
        "user:operator",
        PrincipalKind.HUMAN,
        approval_role_membership=MembershipStatus.VERIFIED_MEMBER,
    )
    single_operator = _payload_authority(
        "prod",
        requester=operator,
        approver=operator,
        prevent_self_review_status=EnvironmentControlStatus.VERIFIED_DISABLED,
    )
    assert _evaluate(population, CloseIntent.PRODUCTION_RETIREMENT, automated).allowed
    assert _evaluate(
        population,
        CloseIntent.PRODUCTION_RETIREMENT,
        automated_without_self_review_guard,
    ).allowed
    assert _evaluate(population, CloseIntent.PRODUCTION_RETIREMENT, single_operator).allowed


@pytest.mark.parametrize(
    "mutation",
    [
        {
            "requester": _principal("shared", PrincipalKind.MACHINE),
            "approver": _principal(
                "shared",
                PrincipalKind.HUMAN,
                approval_role_membership=MembershipStatus.VERIFIED_MEMBER,
            ),
        },
        {"executor": _principal("user:executor", PrincipalKind.HUMAN)},
        {
            "executor": _principal(
                "service:executor",
                PrincipalKind.MACHINE,
                approval_role_membership=MembershipStatus.VERIFIED_MEMBER,
            )
        },
        {
            "executor": _principal(
                "service:executor",
                PrincipalKind.MACHINE,
                admin_membership=MembershipStatus.VERIFIED_MEMBER,
            )
        },
        {
            "executor": _principal(
                "service:executor",
                PrincipalKind.MACHINE,
                admin_membership=MembershipStatus.UNKNOWN,
            )
        },
        {"executor": _principal("user:approver", PrincipalKind.MACHINE)},
    ],
)
def test_production_principal_role_mutations_refuse(mutation):
    population = _population("prod-payload")
    decision = _evaluate(
        population,
        CloseIntent.PRODUCTION_RETIREMENT,
        _payload_authority("prod", **mutation),
    )
    assert not decision.allowed


def _enabled_bypass(**updates):
    values = dict(
        configuration_status=EnvironmentControlStatus.VERIFIED_ENABLED,
        use_status=BypassUseStatus.UNUSED,
        observation_verification_status=VerificationStatus.VERIFIED,
        enabled_bypass_policy_status=VerificationStatus.VERIFIED,
        observation_source="github-environment-deployment-event",
        observation_digest="enabled-bypass-observation-digest",
        actor_principal=None,
    )
    values.update(updates)
    return ProductionBypassEvidence(**values)


def test_enabled_bypass_unused_with_explicit_policy_and_non_admin_bots_allows():
    population = _population("prod-payload")
    authority = _payload_authority("prod", bypass=_enabled_bypass())
    assert _evaluate(population, CloseIntent.PRODUCTION_RETIREMENT, authority).allowed


def test_enabled_bypass_used_by_authorized_human_is_recorded_and_allows():
    population = _population("prod-payload")
    authority = _payload_authority(
        "prod",
        bypass=_enabled_bypass(
            use_status=BypassUseStatus.USED,
            actor_principal="user:approver",
        ),
    )
    assert _evaluate(population, CloseIntent.PRODUCTION_RETIREMENT, authority).allowed


@pytest.mark.parametrize(
    "bypass",
    [
        _enabled_bypass(use_status=BypassUseStatus.USED, actor_principal="bot:requester"),
        _enabled_bypass(use_status=BypassUseStatus.USED, actor_principal=None),
        _enabled_bypass(use_status=BypassUseStatus.UNKNOWN),
        _enabled_bypass(enabled_bypass_policy_status=VerificationStatus.UNKNOWN),
        _enabled_bypass(observation_verification_status=VerificationStatus.UNKNOWN),
        replace(
            _enabled_bypass(),
            configuration_status=EnvironmentControlStatus.UNKNOWN,
        ),
    ],
)
def test_enabled_bypass_wrong_or_unknown_use_refuses(bypass):
    population = _population("prod-payload")
    decision = _evaluate(
        population,
        CloseIntent.PRODUCTION_RETIREMENT,
        _payload_authority("prod", bypass=bypass),
    )
    assert not decision.allowed


def test_enabled_bypass_requires_non_admin_automated_requester():
    population = _population("prod-payload")
    requester = _principal(
        "bot:requester",
        PrincipalKind.MACHINE,
        admin_membership=MembershipStatus.UNKNOWN,
    )
    decision = _evaluate(
        population,
        CloseIntent.PRODUCTION_RETIREMENT,
        _payload_authority("prod", requester=requester, bypass=_enabled_bypass()),
    )
    assert decision.disposition is CloseDisposition.HOLD


def test_external_production_approval_with_equivalent_guarantees_allows():
    population = _population("prod-payload")
    authority = _payload_authority(
        "prod",
        approval_mode=ProductionApprovalMode.EXTERNAL,
        prevent_self_review_status=EnvironmentControlStatus.NOT_APPLICABLE,
        bypass=None,
        policy_reference="change-control-policy:v4",
        approval_verification_source="signed-change-ledger",
        workflow_ref=None,
        workflow_sha=None,
        ref=None,
        verified_workflow_ref=None,
        verified_workflow_sha=None,
        verified_ref=None,
    )
    decision = _evaluate(population, CloseIntent.PRODUCTION_RETIREMENT, authority)
    assert decision.allowed
    assert decision.authority_principal == "service:executor"
    assert decision.authority_digest


def test_external_production_approval_with_github_bypass_evidence_holds():
    population = _population("prod-payload")
    authority = _payload_authority(
        "prod",
        approval_mode=ProductionApprovalMode.EXTERNAL,
        prevent_self_review_status=EnvironmentControlStatus.NOT_APPLICABLE,
        bypass=_enabled_bypass(),
        policy_reference="change-control-policy:v4",
        approval_verification_source="signed-change-ledger",
        workflow_ref=None,
        workflow_sha=None,
        ref=None,
        verified_workflow_ref=None,
        verified_workflow_sha=None,
        verified_ref=None,
    )
    decision = _evaluate(population, CloseIntent.PRODUCTION_RETIREMENT, authority)
    assert not decision.allowed
    assert decision.reason == "external production approval carries GitHub environment controls"


@pytest.mark.parametrize(
    "mutation",
    [
        {"signer_owner": OTHER_OWNER},
        {"repository": "other/repo"},
        {"workload_class": "prod-payload"},
        {"operation_id": "another-operation"},
        {"trust_domain": ""},
        {"source": ""},
        {"evidence_digest": ""},
        {"observed_at": NOW + 1},
        {"valid_until": NOW - 1},
    ],
)
def test_isolated_signer_effect_mutations_hold(mutation):
    decision = _evaluate(
        _population("ci-runner"),
        CloseIntent.CI_CLEANUP,
        _ci_authority(),
        producer_authentication=_isolated(**mutation),
    )
    assert decision.disposition is CloseDisposition.HOLD


@pytest.mark.parametrize(
    ("claim_mutation", "verification_mutation"),
    [
        ({"subject": OTHER_SUBJECT}, {}),
        ({"group_identity_digest": "0" * 64}, {}),
        ({"producer_repository": "other/repo"}, {}),
        ({"repository_id": ""}, {}),
        ({"repository_owner_id": "0"}, {}),
        ({"workflow_sha": "refs/heads/main"}, {}),
        ({"job_workflow_sha": "not-a-commit"}, {}),
        ({"job_workflow_ref": None}, {}),
        ({"job_workflow_sha": None}, {}),
        ({"producer_oidc_jti": ""}, {}),
        ({"issued_at": 0}, {}),
        ({"issued_at": NOW + 1}, {}),
        ({"schema_version": 2}, {}),
        ({}, {"attestation_digest": "0" * 64}),
        ({}, {"trust_root": ""}),
        ({}, {"verified_issuer": "other-issuer"}),
        ({}, {"verified_producer_oidc_subject": "repo:other/repo:ref:refs/heads/main"}),
        ({}, {"verified_producer_oidc_jti": "other-jti"}),
        ({}, {"signature_status": VerificationStatus.FAILED}),
        ({}, {"uniqueness_status": UniquenessStatus.REUSED}),
        ({}, {"producer_jti_uniqueness_status": UniquenessStatus.REUSED}),
        ({}, {"observed_at": NOW + 1}),
        ({}, {"valid_until": NOW - 1}),
    ],
)
def test_shared_signer_claim_and_verification_mutations_hold(
    claim_mutation, verification_mutation
):
    population = _population("ci-runner")
    decision = _evaluate(
        population,
        CloseIntent.CI_CLEANUP,
        _ci_authority(),
        producer_authentication=_shared(population, claim_mutation, verification_mutation),
    )
    assert decision.disposition is CloseDisposition.HOLD


def test_branch_workflow_refs_with_separate_sha_claims_allow_shared_signer():
    population = _population("ci-runner")
    assert _evaluate(
        population,
        CloseIntent.CI_CLEANUP,
        _ci_authority(),
        producer_authentication=_shared(population),
    ).allowed


def test_non_reusable_workflow_attestation_omits_job_workflow_pair():
    population = _population("ci-runner")
    evidence = _shared(
        population,
        claim_updates={"job_workflow_ref": None, "job_workflow_sha": None},
    )

    assert _evaluate(
        population,
        CloseIntent.CI_CLEANUP,
        _ci_authority(),
        producer_authentication=evidence,
    ).allowed


def test_prepared_operation_key_is_independent_of_post_create_dseq():
    parameters = dict(
        signer_owner=SUBJECT.owner,
        producer_repository=REGISTER["example"],
        workload_class="ci-runner",
        operation_id="create-op-123",
        operation_ordinal=1,
        prepared_group_identity_digest=canonical_group_identity_digest(_population("ci-runner")),
        run=12345,
        run_attempt=2,
    )

    assert "subject" not in inspect.signature(canonical_prepared_operation_key).parameters
    assert "dseq" not in inspect.signature(canonical_prepared_operation_key).parameters
    assert canonical_prepared_operation_key(**parameters) == _candidates().prepared_operation_key
    assert canonical_prepared_operation_key(
        **{**parameters, "operation_ordinal": 2}
    ) != canonical_prepared_operation_key(**parameters)


@pytest.mark.parametrize(
    "mutation",
    [
        {"binding_status": CreationBindingStatus.UNKNOWN},
        {"binding_status": CreationBindingStatus.FAILED},
        {"binding_mode": CreationBindingMode.UNKNOWN},
        {"binding_source": ""},
        {"binding_evidence_digest": ""},
        {"backend": ""},
        {"signer_owner": OTHER_OWNER},
        {"group_identity_digest": "0" * 64},
        {"capability_reference": ""},
        {"valid_until": NOW - 31},
    ],
)
def test_creator_rollback_requires_positive_same_operation_binding(mutation):
    population = _population("ci-payload")
    decision = _evaluate(
        population,
        CloseIntent.CREATOR_ROLLBACK,
        _rollback(population, **mutation),
        chain_evidence=None,
        producer_authentication=None,
    )
    assert decision.disposition is CloseDisposition.DENY


@pytest.mark.parametrize("state", [HandoffState.SUCCEEDED, HandoffState.UNKNOWN, "failed"])
def test_creator_rollback_requires_failed_handoff_effect(state):
    population = _population("ci-payload")
    trigger = replace(_rollback(population).rollback_trigger, handoff_state=state)
    decision = _evaluate(
        population,
        CloseIntent.CREATOR_ROLLBACK,
        _rollback(population, rollback_trigger=trigger),
        chain_evidence=None,
        producer_authentication=None,
    )
    assert decision.disposition is CloseDisposition.DENY


@pytest.mark.parametrize(
    "mutation",
    [
        {"repository": "other/repo"},
        {"workload_class": "ci-runner"},
        {"run": 999},
        {"run_attempt": 3},
        {"release": "unexpected"},
        {"expires": 123},
    ],
)
def test_creator_rollback_lifecycle_binding_mutations_deny(mutation):
    population = _population("ci-payload")
    decision = _evaluate(
        population,
        CloseIntent.CREATOR_ROLLBACK,
        _rollback(population, **mutation),
        chain_evidence=None,
        producer_authentication=None,
    )
    assert decision.disposition is CloseDisposition.DENY


def test_creator_rollback_requires_fresh_source_bound_failed_handoff():
    population = _population("ci-payload")
    good = _rollback(population).rollback_trigger
    for trigger in (
        replace(good, source=""),
        replace(good, evidence_digest=""),
        replace(good, observed_at=NOW + 1),
        replace(good, observed_at=NOW - 31),
        replace(good, valid_until=NOW - 1),
    ):
        decision = _evaluate(
            population,
            CloseIntent.CREATOR_ROLLBACK,
            _rollback(population, rollback_trigger=trigger),
            chain_evidence=None,
            producer_authentication=None,
        )
        assert decision.disposition is CloseDisposition.DENY


def test_payload_creator_rollback_binds_original_creation_authorization():
    population = _population("prod-payload")
    authority = _rollback(population)
    assert _evaluate(
        population,
        CloseIntent.CREATOR_ROLLBACK,
        authority,
        chain_evidence=None,
        producer_authentication=_shared(population),
    ).allowed

    for mutated in (
        _rollback(population, creation_authorization_reference="other-approval"),
        authority,
    ):
        candidates = _candidates(
            population,
            creation_authorization_reference=(
                "release-approval-9" if mutated is not authority else "other-approval"
            ),
        )
        decision = _evaluate(
            population,
            CloseIntent.CREATOR_ROLLBACK,
            mutated,
            candidates=candidates,
            chain_evidence=None,
            producer_authentication=_shared(population),
        )
        assert decision.disposition is CloseDisposition.DENY

    decision = _evaluate(
        population,
        CloseIntent.CREATOR_ROLLBACK,
        authority,
        chain_evidence=None,
        producer_authentication=_shared(
            population,
            claim_updates={"authorization_reference": "other-approval"},
        ),
    )
    assert decision.disposition is CloseDisposition.DENY


def test_bound_creator_rollback_can_compensate_without_chain_readback():
    population = _population("ci-payload")
    decision = _evaluate(
        population,
        CloseIntent.CREATOR_ROLLBACK,
        _rollback(population),
        chain_evidence=None,
        producer_authentication=None,
    )
    assert decision.allowed
    assert decision.preclose_evidence_digest is None


def test_generic_force_is_absent_and_cannot_bypass():
    assert "force" not in inspect.signature(evaluate_close).parameters
    with pytest.raises(TypeError, match="force"):
        evaluate_close(
            subject=SUBJECT,
            population=_population("prod-payload"),
            candidates=_candidates(_population("prod-payload")),
            intent=CloseIntent.CI_CLEANUP,
            authority=None,
            chain_evidence=None,
            producer_authentication=None,
            evaluated_at=NOW,
            force=True,
        )


def test_group_and_attestation_digests_are_effect_sensitive():
    population = _population("ci-runner")
    reversed_population = replace(population, identities=tuple(reversed(population.identities)))
    changed_population = replace(
        population,
        identities=tuple(replace(item, attempt=3) for item in population.identities),
    )
    assert canonical_group_identity_digest(reversed_population) == canonical_group_identity_digest(
        population
    )
    assert canonical_group_identity_digest(changed_population) != canonical_group_identity_digest(
        population
    )
    claim = _claim(population)
    assert canonical_attestation_digest(claim) != canonical_attestation_digest(
        replace(claim, operation_id="other")
    )


def test_policy_call_site_reaches_authorizer_once(monkeypatch):
    source = inspect.getsource(evaluate_close)
    target = "return _authorize("
    assert source.count(target) == 1
    planted = CloseDecision(
        CloseDisposition.DENY,
        CloseIntent.CI_CLEANUP,
        SUBJECT,
        CloseReasonCode.AUTHORITY_INVALID,
        "planted call-site effect",
    )
    calls = []

    def fake_authorize(**kwargs):
        calls.append(kwargs)
        return planted

    monkeypatch.setattr(policy_module, "_authorize", fake_authorize)
    decision = _evaluate(_population("ci-runner"), CloseIntent.CI_CLEANUP, _ci_authority())
    assert decision is planted
    assert len(calls) == 1


def test_close_reason_code_population_is_stable_and_grouped():
    assert CLOSE_POLICY_VERSION == 2
    assert tuple(reason.value for reason in CloseReasonCode) == (
        "classification.incomplete",
        "classification.held",
        "classification.count_mismatch",
        "classification.lifecycle_mixed",
        "candidate.incomplete_or_non_unique",
        "class_policy.forbids_intent",
        "producer_authentication.missing",
        "producer_authentication.shared_invalid",
        "producer_authentication.isolated_invalid",
        "chain_evidence.missing",
        "chain_evidence.wrong_population",
        "chain_evidence.trust_paths_incomplete",
        "chain_evidence.digest_or_interval_incomplete",
        "chain_evidence.population_mismatch",
        "chain_evidence.lease_population_incomplete",
        "chain_evidence.sources_disagree",
        "lifecycle_authority.missing",
        "lifecycle_authority.evidence_incomplete",
        "lifecycle_authority.invalid",
        "lifecycle_authority.wrong_type",
        "lifecycle_authority.identity_disagreement",
        "authorization.succeeded",
    )


def test_close_decision_rejects_untyped_codes_and_success_mismatches():
    with pytest.raises(ValueError, match="typed reason code"):
        CloseDecision(
            CloseDisposition.DENY,
            CloseIntent.CI_CLEANUP,
            SUBJECT,
            "lifecycle_authority.invalid",
            "diagnostic",
        )
    with pytest.raises(ValueError, match="success code"):
        CloseDecision(
            CloseDisposition.DENY,
            CloseIntent.CI_CLEANUP,
            SUBJECT,
            CloseReasonCode.AUTHORIZATION_SUCCEEDED,
            "diagnostic",
        )


def test_every_reason_code_is_reached_by_a_real_policy_path():
    population = _population("ci-runner")

    def classify_only(mutated_population):
        return evaluate_close(
            subject=SUBJECT,
            population=mutated_population,
            candidates=_candidates(population),
            intent=CloseIntent.CI_CLEANUP,
            authority=None,
            chain_evidence=None,
            producer_authentication=None,
            evaluated_at=NOW,
        )

    reached = {
        classify_only(
            replace(
                population,
                completeness=PopulationCompleteness.INCOMPLETE,
            )
        ).reason_code,
        classify_only(replace(population, held=True)).reason_code,
        classify_only(replace(population, parsed_count=1)).reason_code,
        classify_only(
            replace(
                population,
                identities=(
                    population.identities[0],
                    replace(population.identities[1], attempt=3),
                ),
            )
        ).reason_code,
        _evaluate(
            _population("prod-payload"),
            CloseIntent.CI_CLEANUP,
            None,
            chain_evidence=None,
            producer_authentication=None,
        ).reason_code,
        _evaluate(
            population,
            CloseIntent.CI_CLEANUP,
            _ci_authority(),
            candidates=_candidates(population, count=0),
        ).reason_code,
        _evaluate(
            population,
            CloseIntent.CI_CLEANUP,
            _ci_authority(),
            producer_authentication=None,
        ).reason_code,
        _evaluate(
            population,
            CloseIntent.CI_CLEANUP,
            _ci_authority(),
            producer_authentication=_shared(
                population,
                claim_updates={"subject": OTHER_SUBJECT},
            ),
        ).reason_code,
        _evaluate(
            population,
            CloseIntent.CI_CLEANUP,
            _ci_authority(),
            producer_authentication=_isolated(operation_id="other"),
        ).reason_code,
        _evaluate(
            population,
            CloseIntent.CI_CLEANUP,
            _ci_authority(),
            chain_evidence=None,
        ).reason_code,
        _evaluate(
            population,
            CloseIntent.CI_CLEANUP,
            _ci_authority(),
            chain_evidence=_chain(population, subject=OTHER_SUBJECT),
        ).reason_code,
        _evaluate(
            population,
            CloseIntent.CI_CLEANUP,
            _ci_authority(),
            chain_evidence=_chain(population, source_b="rpc-a.example"),
        ).reason_code,
        _evaluate(
            population,
            CloseIntent.CI_CLEANUP,
            _ci_authority(),
            chain_evidence=_chain(population, evidence_digest=""),
        ).reason_code,
        _evaluate(
            population,
            CloseIntent.CI_CLEANUP,
            _ci_authority(),
            chain_evidence=_chain(population, deployment_count=0),
        ).reason_code,
        _evaluate(
            population,
            CloseIntent.CI_CLEANUP,
            _ci_authority(),
            chain_evidence=_chain(population, lease_count=0),
        ).reason_code,
        _evaluate(
            population,
            CloseIntent.CI_CLEANUP,
            _ci_authority(),
            chain_evidence=_chain(population, agreement=SourceAgreement.DISAGREEING),
        ).reason_code,
        _evaluate(population, CloseIntent.CI_CLEANUP, None).reason_code,
        _evaluate(
            population,
            CloseIntent.CI_CLEANUP,
            _ci_authority(source=""),
        ).reason_code,
        _evaluate(
            population,
            CloseIntent.CI_CLEANUP,
            _ci_authority(run_state=RunState.LIVE),
        ).reason_code,
        _evaluate(
            population,
            CloseIntent.CI_CLEANUP,
            _payload_authority("staging"),
        ).reason_code,
        _evaluate(
            population,
            CloseIntent.CI_CLEANUP,
            _ci_authority(run=999),
        ).reason_code,
        _evaluate(population, CloseIntent.CI_CLEANUP, _ci_authority()).reason_code,
    }

    assert reached == set(CloseReasonCode)


def test_every_close_decision_call_site_supplies_a_machine_code_and_message():
    tree = ast.parse(inspect.getsource(policy_module))
    functions = {node.name: node for node in tree.body if isinstance(node, ast.FunctionDef)}
    direct_calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "_decision"
    ]
    result_calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "result"
    ]
    reason_returns = [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Return)
        and isinstance(node.value, ast.Call)
        and isinstance(node.value.func, ast.Name)
        and node.value.func.id in {"_Reason", "_authority_hold", "_authority_deny"}
    ]

    assert len(direct_calls) == 15
    assert len(result_calls) == 23
    assert len(reason_returns) == 72
    assert all(len(call.args) >= 7 for call in direct_calls)
    assert all(len(call.args) == 3 for call in result_calls)

    def returned_values(function_name):
        return [
            node.value
            for node in ast.walk(functions[function_name])
            if isinstance(node, ast.Return)
        ]

    def is_call(value, *names):
        return (
            isinstance(value, ast.Call)
            and isinstance(value.func, ast.Name)
            and value.func.id in names
        )

    def is_none(value):
        return isinstance(value, ast.Constant) and value.value is None

    for function_name in ("_shared_reason", "_isolated_reason", "_class_denial"):
        assert all(
            is_none(value) or is_call(value, "_Reason") for value in returned_values(function_name)
        )
    assert all(
        is_none(value) or is_call(value, "_authority_hold", "_authority_deny")
        for value in returned_values("_authority_reason")
    )
    assert all(
        isinstance(value, ast.Tuple)
        and (is_none(value.elts[1]) or is_call(value.elts[1], "_Reason"))
        for value in returned_values("_classified")
    )


def test_message_mutation_cannot_change_machine_adapter_behavior(monkeypatch):
    def adapter_action(decision):
        if decision.reason_code is CloseReasonCode.AUTHORIZATION_SUCCEEDED:
            return "close"
        if decision.reason_code is CloseReasonCode.AUTHORITY_EVIDENCE_INCOMPLETE:
            return "retry-evidence"
        return "refuse"

    population = _population("ci-runner")
    allowed = _evaluate(population, CloseIntent.CI_CLEANUP, _ci_authority())
    held = _evaluate(
        population,
        CloseIntent.CI_CLEANUP,
        _ci_authority(source=""),
    )
    mutated = replace(held, message="presentation text changed completely")

    assert adapter_action(allowed) == "close"
    assert adapter_action(held) == adapter_action(mutated) == "retry-evidence"
    assert held.reason_code is CloseReasonCode.AUTHORITY_EVIDENCE_INCOMPLETE

    original = policy_module._authority_hold

    def changed_message(message):
        result = original(message)
        return replace(result, message="another diagnostic")

    monkeypatch.setattr(policy_module, "_authority_hold", changed_message)
    changed = _evaluate(
        population,
        CloseIntent.CI_CLEANUP,
        _ci_authority(source=""),
    )
    assert changed.disposition is CloseDisposition.HOLD
    assert changed.reason_code is CloseReasonCode.AUTHORITY_EVIDENCE_INCOMPLETE
    assert adapter_action(changed) == "retry-evidence"
