from dataclasses import FrozenInstanceError, replace

import pytest

from akash_lease_core import (
    BackendIdentity,
    BackendKind,
    BoundDeployment,
    CreateJournal,
    CreateOutcome,
    CreateOutcomeKind,
    CreatorRollbackRequest,
    DeploymentKey,
    ExactChainReadEvidence,
    ExecutionClosure,
    HandoffFailure,
    JournalEntry,
    JournalState,
    LifecycleIdentity,
    NonCommitEvidence,
    OutcomeReasonCode,
    OwnerCandidate,
    OwnerEvidenceKind,
    PreparedCreate,
    PreparedGroup,
    ProducerProvenance,
    RejectionEvidence,
    SettlementEvidence,
    SettlementState,
    SubmissionEvidence,
    TransactionEventEvidence,
    TransitionError,
    UncertaintyEvidence,
    append_transition,
    canonical_group_population_digest,
    canonical_journal_bytes,
    canonical_journal_digest,
    canonical_payload_digest,
)

OWNER = "akash1n4uut3vxmkdp8wsrya3q0qyddgqey0rh9as4ee"
NOW = 1_800_000_000
OPERATION = "create:example/repo:42:1"
GROUPS = (
    PreparedGroup(1, "example-idv1-class-ci-runner-g1-attempt-1-run-42-end"),
    PreparedGroup(2, "example-idv1-class-ci-runner-g2-attempt-1-run-42-end"),
)
GROUP_DIGEST = canonical_group_population_digest(GROUPS)
REQUEST = b'{"sdl":"canonical request"}'
SIGNED = b"canonical signed transaction"
MEDIATED_BACKEND = BackendIdentity(BackendKind.MEDIATED, "console:production")
SELF_MANAGED_BACKEND = BackendIdentity(BackendKind.SELF_MANAGED, "cli:local-signer")


def _producer(**changes):
    value = ProducerProvenance(
        repository="example/repo",
        repository_id="123",
        repository_owner_id="456",
        issuer="https://token.actions.githubusercontent.com",
        audience="akash-create",
        subject="repo:example/repo:ref:refs/heads/main",
        workflow_ref="example/repo/.github/workflows/deploy.yml@refs/heads/main",
        workflow_sha="a" * 40,
        ref="refs/heads/main",
        jti="oidc-jti-1",
        authentication_evidence_digest="1" * 64,
    )
    return replace(value, **changes)


def _lifecycle(**changes):
    value = LifecycleIdentity(
        repository="example/repo", workload_class="ci-runner", run=42, run_attempt=1
    )
    return replace(value, **changes)


def _owner_candidate(**changes):
    value = OwnerCandidate(
        backend=MEDIATED_BACKEND,
        owner=OWNER,
        evidence_kind=OwnerEvidenceKind.AUTHENTICATED_MEDIATOR,
        evidence_source="console authenticated account endpoint",
        evidence_digest="2" * 64,
    )
    return replace(value, **changes)


def _prepared(**changes):
    value = PreparedCreate(
        operation_id=OPERATION,
        operation_ordinal=1,
        producer=_producer(),
        lifecycle=_lifecycle(),
        owner_candidate=_owner_candidate(),
        request_digest=canonical_payload_digest(REQUEST),
        sdl_digest="3" * 64,
        groups=GROUPS,
        group_population_digest=GROUP_DIGEST,
        source_revision="b" * 40,
        prepared_at=NOW,
    )
    return replace(value, **changes)


def _submission(**changes):
    value = SubmissionEvidence(
        operation_id=OPERATION,
        backend=MEDIATED_BACKEND,
        submitted_request_digest=canonical_payload_digest(REQUEST),
        submitted_at=NOW + 1,
        evidence_source="console response headers",
        evidence_digest="4" * 64,
        submission_token="submission-123",
    )
    return replace(value, **changes)


def _chain_read(**changes):
    subject = DeploymentKey(OWNER, "9001")
    value = ExactChainReadEvidence(
        operation_id=OPERATION,
        subject=subject,
        chain_id="akashnet-2",
        block_height=123456,
        groups=GROUPS,
        group_population_digest=GROUP_DIGEST,
        source="direct RPC deployment read",
        evidence_digest="5" * 64,
        observed_at=NOW + 2,
    )
    return replace(value, **changes)


def _bound(**changes):
    value = BoundDeployment(
        operation_id=OPERATION,
        subject=DeploymentKey(OWNER, "9001"),
        groups=GROUPS,
        group_population_digest=GROUP_DIGEST,
        binding_evidence=_chain_read(),
    )
    return replace(value, **changes)


def _committed(**changes):
    value = CreateOutcome(
        operation_id=OPERATION,
        kind=CreateOutcomeKind.COMMITTED,
        reason_code=OutcomeReasonCode.EXACT_CHAIN_READ_BOUND,
        deployment=_bound(),
    )
    return replace(value, **changes)


def _prepared_journal():
    return CreateJournal().append(JournalState.PREPARED, _prepared(), recorded_at=NOW)


def _created_journal():
    journal = _prepared_journal()
    journal = journal.append(JournalState.SUBMITTED, _submission(), recorded_at=NOW + 1)
    return journal.append(JournalState.CREATED, _committed(), recorded_at=NOW + 2)


def test_values_and_journal_are_immutable():
    prepared = _prepared()
    with pytest.raises(FrozenInstanceError):
        prepared.operation_id = "changed"
    with pytest.raises(FrozenInstanceError):
        _prepared_journal().entries = ()


def test_canonical_serialization_and_digest_are_stable():
    value = PreparedGroup(2, "group-two")
    assert canonical_journal_bytes(value) == (
        b'{"$type":"PreparedGroup","group_name":"group-two","gseq":2}'
    )
    assert canonical_journal_digest(value) == (
        "04106d17a805504cf3bf9ff0c73c0a7ae0303d4066f5331610aa7b6abef5be31"
    )
    assert canonical_payload_digest(b"abc") == (
        "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
    )


def test_group_digest_is_ordered_and_requires_complete_one_to_n_population():
    assert canonical_group_population_digest(GROUPS) == GROUP_DIGEST
    with pytest.raises(ValueError, match="ordered complete"):
        canonical_group_population_digest(tuple(reversed(GROUPS)))
    with pytest.raises(ValueError, match="ordered complete"):
        canonical_group_population_digest((GROUPS[1],))


def test_complete_recovery_path_is_append_only_and_keeps_settlement_separate():
    journal = _created_journal()
    subject = _bound().subject
    journal = append_transition(
        journal,
        JournalState.HANDOFF_FAILED,
        HandoffFailure(OPERATION, subject, "handoff", "6" * 64, NOW + 3),
        recorded_at=NOW + 3,
    )
    journal = journal.append(
        JournalState.CREATOR_ROLLBACK_REQUESTED,
        CreatorRollbackRequest(
            OPERATION, subject, "close-1", "creator capability", "7" * 64, NOW + 4
        ),
        recorded_at=NOW + 4,
    )
    journal = journal.append(
        JournalState.EXECUTION_CLOSED,
        ExecutionClosure(
            OPERATION,
            subject,
            "rpc-a",
            "rpc-b",
            GROUP_DIGEST,
            "8" * 64,
            "9" * 64,
            NOW + 5,
        ),
        recorded_at=NOW + 5,
    )
    for offset, state in enumerate(
        (SettlementState.UNMEASURED, SettlementState.UNSETTLED, SettlementState.SETTLED),
        start=6,
    ):
        journal = journal.append(
            JournalState.SETTLEMENT,
            SettlementEvidence(
                OPERATION, subject, state, "rpc-a", "rpc-b", "a" * 64, NOW + offset
            ),
            recorded_at=NOW + offset,
        )

    assert tuple(entry.state for entry in journal.entries) == (
        JournalState.PREPARED,
        JournalState.SUBMITTED,
        JournalState.CREATED,
        JournalState.HANDOFF_FAILED,
        JournalState.CREATOR_ROLLBACK_REQUESTED,
        JournalState.EXECUTION_CLOSED,
        JournalState.SETTLEMENT,
        JournalState.SETTLEMENT,
        JournalState.SETTLEMENT,
    )
    assert journal.entries[-1].payload.state is SettlementState.SETTLED


def test_unknown_create_can_only_reconcile_to_the_prepared_owner_and_groups():
    journal = _prepared_journal().append(
        JournalState.CREATE_OUTCOME_UNKNOWN,
        CreateOutcome(
            OPERATION,
            CreateOutcomeKind.UNKNOWN,
            OutcomeReasonCode.STARTED_PROCESS_TIMEOUT,
            uncertainty=UncertaintyEvidence(
                OPERATION, "create process", "a" * 64, NOW + 1, response_dseq="9001"
            ),
        ),
        recorded_at=NOW + 1,
    )
    journal = journal.append(JournalState.CREATED, _committed(), recorded_at=NOW + 2)
    assert journal.entries[-1].payload.deployment.subject == DeploymentKey(OWNER, "9001")

    changed_groups = (replace(GROUPS[0], group_name="other"), GROUPS[1])
    changed_digest = canonical_group_population_digest(changed_groups)
    changed_read = _chain_read(groups=changed_groups, group_population_digest=changed_digest)
    changed_bound = _bound(
        groups=changed_groups,
        group_population_digest=changed_digest,
        binding_evidence=changed_read,
    )
    with pytest.raises(TransitionError, match="prepared owner or groups"):
        journal.entries[:2] and CreateJournal(journal.entries[:2]).append(
            JournalState.CREATED,
            _committed(deployment=changed_bound),
            recorded_at=NOW + 2,
        )


@pytest.mark.parametrize(
    "reason",
    [
        OutcomeReasonCode.BROAD_EXCEPTION,
        OutcomeReasonCode.STARTED_PROCESS_TIMEOUT,
        OutcomeReasonCode.HTTP_READ_TIMEOUT,
        OutcomeReasonCode.HTTP_WRITE_TIMEOUT,
        OutcomeReasonCode.TRANSPORT_ERROR,
        OutcomeReasonCode.RESPONSE_LOST,
        OutcomeReasonCode.BACKEND_ROTATION,
        OutcomeReasonCode.PROVIDER_ROTATION,
        OutcomeReasonCode.RECONCILIATION_INCOMPLETE,
    ],
)
def test_ambiguous_failures_have_only_unknown_outcomes(reason):
    outcome = CreateOutcome(
        OPERATION,
        CreateOutcomeKind.UNKNOWN,
        reason,
        uncertainty=UncertaintyEvidence(OPERATION, "adapter", "a" * 64, NOW + 1),
    )
    assert outcome.kind is CreateOutcomeKind.UNKNOWN
    with pytest.raises(ValueError, match="reason or evidence"):
        replace(outcome, kind=CreateOutcomeKind.FALLBACK_SAFE)


def test_rejected_before_send_requires_a_separate_non_commit_proof_before_fallback():
    rejection = CreateOutcome(
        OPERATION,
        CreateOutcomeKind.REJECTED_BEFORE_SEND,
        OutcomeReasonCode.MEDIATOR_REJECTED,
        rejection=RejectionEvidence(
            OPERATION, MEDIATED_BACKEND, True, "mediator", "b" * 64, NOW + 1
        ),
    )
    journal = _prepared_journal().append(
        JournalState.REJECTED_BEFORE_SEND, rejection, recorded_at=NOW + 1
    )
    safe = CreateOutcome(
        OPERATION,
        CreateOutcomeKind.FALLBACK_SAFE,
        OutcomeReasonCode.SUBMISSION_PROVEN_ABSENT,
        non_commit=NonCommitEvidence(
            OPERATION,
            MEDIATED_BACKEND,
            None,
            None,
            OWNER,
            GROUP_DIGEST,
            "submission ledger",
            "direct chain",
            "c" * 64,
            NOW + 2,
        ),
    )
    assert journal.append(JournalState.FALLBACK_SAFE, safe, recorded_at=NOW + 2)


def test_reason_codes_are_stable_and_exhaustive():
    assert tuple(reason.value for reason in OutcomeReasonCode) == (
        "intent_rejected",
        "signing_rejected",
        "mediator_rejected",
        "submission_proven_absent",
        "deployment_proven_absent",
        "transaction_event_bound",
        "exact_chain_read_bound",
        "broad_exception",
        "started_process_timeout",
        "http_read_timeout",
        "http_write_timeout",
        "transport_error",
        "response_lost",
        "backend_rotation",
        "provider_rotation",
        "reconciliation_incomplete",
    )


def test_transition_table_rejects_skips_duplicates_and_time_rewrites():
    with pytest.raises(TransitionError, match="first operation transition"):
        CreateJournal().append(JournalState.SUBMITTED, _submission(), recorded_at=NOW + 1)
    with pytest.raises(TransitionError, match="invalid transition"):
        _prepared_journal().append(JournalState.CREATED, _committed(), recorded_at=NOW + 2)
    with pytest.raises(TransitionError, match="duplicate operation ID"):
        _prepared_journal().append(JournalState.PREPARED, _prepared(), recorded_at=NOW + 1)
    with pytest.raises(TransitionError, match="predates|backwards"):
        _prepared_journal().append(JournalState.SUBMITTED, _submission(), recorded_at=NOW - 1)


def test_lifecycle_ordinal_allocation_and_exact_backend_are_not_reusable():
    second_operation = replace(_prepared(), operation_id="second-operation")
    with pytest.raises(TransitionError, match="duplicate operation ordinal"):
        _prepared_journal().append(JournalState.PREPARED, second_operation, recorded_at=NOW + 1)

    rotated = _submission(backend=BackendIdentity(BackendKind.MEDIATED, "console:rotated-backend"))
    with pytest.raises(TransitionError, match="prepared request or backend"):
        _prepared_journal().append(JournalState.SUBMITTED, rotated, recorded_at=NOW + 1)


def test_consumer_cannot_bypass_validation_by_constructing_a_journal_directly():
    skipped = JournalEntry(
        revision=1,
        operation_id=OPERATION,
        state=JournalState.CREATED,
        payload=_committed(),
        recorded_at=NOW + 2,
        previous_entry_digest=None,
    )
    with pytest.raises(TransitionError, match="first operation transition"):
        CreateJournal((skipped,))


def test_rewrite_of_every_required_prepared_field_breaks_the_entry_digest():
    entry = _prepared_journal().entries[0]
    altered_groups = (replace(GROUPS[0], group_name="changed"), GROUPS[1])
    mutations = {
        "operation_id": "another-operation",
        "operation_ordinal": 2,
        "producer": _producer(jti="other-jti"),
        "lifecycle": _lifecycle(run=43),
        "owner_candidate": _owner_candidate(evidence_source="other authenticated source"),
        "request_digest": "b" * 64,
        "sdl_digest": "c" * 64,
        "groups": altered_groups,
        "group_population_digest": canonical_group_population_digest(altered_groups),
        "source_revision": "c" * 40,
        "prepared_at": NOW - 1,
    }
    for field, changed in mutations.items():
        updates = {field: changed}
        if field == "groups":
            updates["group_population_digest"] = canonical_group_population_digest(changed)
        if field == "group_population_digest":
            updates["groups"] = altered_groups
        mutated_payload = replace(entry.payload, **updates)
        with pytest.raises(TransitionError, match="rewritten"):
            replace(entry, payload=mutated_payload)


def test_second_deployment_cannot_bind_to_one_operation_or_two_operations():
    journal = _created_journal()
    second_subject = DeploymentKey(OWNER, "9002")
    second_read = _chain_read(subject=second_subject)
    second = _committed(deployment=_bound(subject=second_subject, binding_evidence=second_read))
    with pytest.raises(TransitionError, match="invalid transition"):
        journal.append(JournalState.CREATED, second, recorded_at=NOW + 3)

    second_operation = "create:example/repo:42:2"
    second_prepared = replace(_prepared(), operation_id=second_operation, operation_ordinal=2)
    journal = journal.append(JournalState.PREPARED, second_prepared, recorded_at=NOW + 3)
    second_unknown = CreateOutcome(
        second_operation,
        CreateOutcomeKind.UNKNOWN,
        OutcomeReasonCode.RESPONSE_LOST,
        uncertainty=UncertaintyEvidence(second_operation, "mediator", "b" * 64, NOW + 4),
    )
    journal = journal.append(
        JournalState.CREATE_OUTCOME_UNKNOWN, second_unknown, recorded_at=NOW + 4
    )
    second_operation_read = replace(_chain_read(), operation_id=second_operation)
    duplicate_binding = replace(
        _bound(), operation_id=second_operation, binding_evidence=second_operation_read
    )
    duplicate_outcome = replace(
        _committed(), operation_id=second_operation, deployment=duplicate_binding
    )
    with pytest.raises(TransitionError, match="two operations"):
        journal.append(JournalState.CREATED, duplicate_outcome, recorded_at=NOW + 5)


def test_locally_copied_owner_and_dseq_are_not_binding_evidence():
    with pytest.raises(ValueError, match="not chain binding evidence"):
        BoundDeployment(OPERATION, DeploymentKey(OWNER, "9001"), GROUPS, GROUP_DIGEST, object())


def test_transaction_event_must_match_exact_submitted_signed_bytes():
    tx_hash = canonical_payload_digest(SIGNED)
    self_managed_prepared = replace(
        _prepared(),
        owner_candidate=OwnerCandidate(
            SELF_MANAGED_BACKEND,
            OWNER,
            OwnerEvidenceKind.VERIFIED_SIGNER,
            "local signer challenge",
            "d" * 64,
        ),
    )
    submission = SubmissionEvidence(
        OPERATION,
        SELF_MANAGED_BACKEND,
        canonical_payload_digest(REQUEST),
        NOW + 1,
        "broadcast client",
        "e" * 64,
        transaction_hash=tx_hash,
        account_sequence=7,
        signed_bytes=SIGNED,
    )
    journal = CreateJournal().append(JournalState.PREPARED, self_managed_prepared, recorded_at=NOW)
    journal = journal.append(JournalState.SUBMITTED, submission, recorded_at=NOW + 1)
    subject = DeploymentKey(OWNER, "9001")
    event = TransactionEventEvidence(
        OPERATION,
        subject,
        "f" * 64,
        0,
        123,
        "akashnet-2",
        GROUP_DIGEST,
        "tx result",
        "1" * 64,
    )
    bound = BoundDeployment(OPERATION, subject, GROUPS, GROUP_DIGEST, event)
    outcome = CreateOutcome(
        OPERATION,
        CreateOutcomeKind.COMMITTED,
        OutcomeReasonCode.TRANSACTION_EVENT_BOUND,
        deployment=bound,
    )
    with pytest.raises(TransitionError, match="submitted transaction"):
        journal.append(JournalState.CREATED, outcome, recorded_at=NOW + 2)
