"""Exercise the recovery boundary with the existing lifecycle fixtures."""

import json
import traceback

import pytest
import test_create_journal as journal_fixture
import test_creation_admission as admission_fixture

from akash_lease_core import (
    AdmissionDisposition,
    CreateJournal,
    CreatorRollbackRequest,
    HandoffFailure,
    JournalState,
    SettlementEvidence,
    SettlementState,
    canonical_journal_bytes,
    canonical_journal_digest,
    reserve_capacity,
    transition_reservation,
)
from akash_lease_core.recovery_codec import (
    MAX_RECOVERY_BYTES,
    RecoveryDecodeError,
    decode_admission_request,
    decode_admission_state,
    decode_create_journal,
    decode_prepared_create,
)


def _canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(",", ":")).encode()


def _roundtrip(value, decoder):
    raw = canonical_journal_bytes(value)
    recovered = decoder(raw)
    assert recovered == value
    assert canonical_journal_bytes(recovered) == raw
    assert canonical_journal_digest(recovered) == canonical_journal_digest(value)
    return recovered


def test_complete_journal_recovery_keeps_all_transitions_and_separate_settlement():
    f = journal_fixture
    journal = f._created_journal()
    subject = f._bound().subject
    journal = (
        journal.append(
            JournalState.HANDOFF_FAILED,
            HandoffFailure(f.OPERATION, subject, "handoff", "6" * 64, f.NOW + 3),
            recorded_at=f.NOW + 3,
        )
        .append(
            JournalState.CREATOR_ROLLBACK_REQUESTED,
            CreatorRollbackRequest(
                f.OPERATION, subject, "close", "creator proof", "7" * 64, f.NOW + 4
            ),
            recorded_at=f.NOW + 4,
        )
        .append(JournalState.EXECUTION_CLOSED, f._execution_closure(), recorded_at=f.NOW + 5)
    )
    for offset, state in enumerate(SettlementState, 6):
        journal = journal.append(
            JournalState.SETTLEMENT,
            SettlementEvidence(
                f.OPERATION, subject, state, "rpc-a", "rpc-b", "a" * 64, f.NOW + offset
            ),
            recorded_at=f.NOW + offset,
        )
    recovered = _roundtrip(journal, decode_create_journal)
    assert recovered.revision == 9
    assert isinstance(recovered.entries[-1].payload, SettlementEvidence)
    assert recovered.entries[-1].payload.state is SettlementState.SETTLED


def test_signed_submission_bytes_survive_storage_without_reinterpretation():
    f = journal_fixture
    prepared = f._prepared(
        owner_candidate=f._owner_candidate(
            backend=f.SELF_MANAGED_BACKEND, evidence_kind=f.OwnerEvidenceKind.VERIFIED_SIGNER
        )
    )
    signed = bytes(range(256))
    submission = f._submission(
        backend=f.SELF_MANAGED_BACKEND,
        signed_bytes=signed,
        transaction_hash=f.canonical_payload_digest(signed),
        account_sequence=0,
        submission_token=None,
    )
    journal = CreateJournal().append(JournalState.PREPARED, prepared, recorded_at=f.NOW)
    journal = journal.append(JournalState.SUBMITTED, submission, recorded_at=f.NOW + 1)
    recovered = _roundtrip(journal, decode_create_journal)
    assert recovered.entries[1].payload.signed_bytes == signed


def test_restart_keeps_unknown_reserved_populations_and_duplicate_is_reconciliation_only():
    f = admission_fixture
    request = f._request()
    submitting = f._submitting(request)
    unknown = transition_reservation(submitting, f._unknown(request), expected_revision=2)
    recovered = _roundtrip(unknown, decode_admission_state)
    held = recovered.reservations[0]
    assert (held.active_slots, held.unresolved_slots, held.financial_exposure_uact) == (1, 1, 100)
    duplicate = reserve_capacity(
        recovered, request, expected_revision=recovered.revision, now=f.NOW + 3
    )
    assert duplicate.decision.disposition is AdmissionDisposition.RECONCILE
    assert duplicate.proposal is None


def test_restart_keeps_execution_closed_exposure_until_separate_settlement():
    f = admission_fixture
    request = f._request()
    state = transition_reservation(
        f._submitting(request), f._committed(request), expected_revision=2
    )
    state = transition_reservation(state, f._closure(request), expected_revision=3)
    recovered = _roundtrip(state, decode_admission_state)
    assert recovered.reservations[0].financial_exposure_uact == 100
    settled = transition_reservation(recovered, f._settlement(request), expected_revision=4)
    assert _roundtrip(settled, decode_admission_state).reservations[0].financial_exposure_uact == 0


@pytest.mark.parametrize(
    "value,decoder",
    [
        (CreateJournal(), decode_create_journal),
        (journal_fixture._prepared(), decode_prepared_create),
        (admission_fixture._request(), decode_admission_request),
        (admission_fixture._state(), decode_admission_state),
        (admission_fixture._proposal().proposed_state, decode_admission_state),
    ],
)
def test_supported_roots_roundtrip(value, decoder):
    _roundtrip(value, decoder)


@pytest.mark.parametrize(
    "decoder",
    [
        decode_create_journal,
        decode_admission_state,
        decode_prepared_create,
        decode_admission_request,
    ],
)
def test_decoders_refuse_issued_authority_and_proposals(decoder):
    f = admission_fixture
    permit = f._permit()
    for value in (permit, f._proposal()):
        with pytest.raises(RecoveryDecodeError):
            decoder(canonical_journal_bytes(value))


@pytest.mark.parametrize(
    "field,value",
    [
        ("schema_version", 2),
        ("schema_version", True),
        ("revision", True),
        ("revision", 1.0),
        ("revision", "1"),
        ("revision", -1),
        ("$type", "CreateSubmissionAuthorization"),
        ("$type", "os.system"),
        ("$type", []),
        ("extra", "field"),
    ],
)
def test_invalid_schema_primitives_tags_or_extra_fields_hold(field, value):
    raw = json.loads(canonical_journal_bytes(admission_fixture._state()))
    raw[field] = value
    with pytest.raises(RecoveryDecodeError):
        decode_admission_state(_canonical(raw))


@pytest.mark.parametrize("mutation", ["missing", "enum", "nested_tag", "binding", "bool_gseq"])
def test_nested_data_is_revalidated_against_its_declared_model(mutation):
    raw = json.loads(canonical_journal_bytes(admission_fixture._request()))
    if mutation == "missing":
        del raw["prepared"]["producer"]["authentication_evidence_digest"]
    elif mutation == "enum":
        raw["prepared"]["owner_candidate"]["backend"]["kind"] = "invented"
    elif mutation == "nested_tag":
        raw["prepared"]["producer"]["$type"] = "PreparedGroup"
    elif mutation == "binding":
        raw["exposure"]["prepared_operation_digest"] = "0" * 64
    else:
        raw["prepared"]["groups"][0]["gseq"] = True
    with pytest.raises(RecoveryDecodeError):
        decode_admission_request(_canonical(raw))


@pytest.mark.parametrize(
    "mutation", ["hash", "link", "ordinal", "skip", "reorder", "empty_digest"]
)
def test_history_rewrite_or_illegal_transition_does_not_survive_restart(mutation):
    raw = json.loads(canonical_journal_bytes(journal_fixture._created_journal()))
    if mutation == "hash":
        raw["entries"][0]["entry_digest"] = "0" * 64
    elif mutation == "link":
        raw["entries"][1]["previous_entry_digest"] = None
    elif mutation == "ordinal":
        raw["entries"][0]["payload"]["operation_ordinal"] = 2
    elif mutation == "skip":
        raw["entries"] = raw["entries"][1:]
    elif mutation == "reorder":
        raw["entries"].reverse()
    else:
        raw["entries"][0]["entry_digest"] = ""
    with pytest.raises(RecoveryDecodeError):
        decode_create_journal(_canonical(raw))


@pytest.mark.parametrize(
    "payload",
    [
        b"",
        "{}",
        bytearray(b"{}"),
        b"{",
        b"[]",
        b"null",
        b'{"$type":"CreateJournal","entries":[],"schema_version":1,"schema_version":1}',
        b'{"$type":"CreateJournal","entries":[],"schema_version":NaN}',
        b'{"$type":"CreateJournal","entries":[],"schema_version":Infinity}',
        b' {"$type":"CreateJournal","entries":[],"schema_version":1}',
        b'{"schema_version":1,"entries":[],"$type":"CreateJournal"}',
        b"x" * (MAX_RECOVERY_BYTES + 1),
        b"[" * 2000 + b"]" * 2000,
        b"[" * 65 + b"]" * 65,
        b"[" + b"0," * 100000 + b"0]",
    ],
)
def test_malformed_noncanonical_or_oversized_input_is_bounded(payload):
    with pytest.raises(RecoveryDecodeError):
        decode_create_journal(payload)


def test_decoder_traceback_does_not_print_stored_secret_content():
    secret = b"stored-credential-do-not-display"
    try:
        decode_create_journal(b"\xff" + secret)
    except RecoveryDecodeError as exc:
        assert secret.decode() not in "".join(traceback.format_exception(exc))
        assert str(exc) == "invalid canonical recovery value"
    else:
        pytest.fail("malformed UTF-8 accepted")


@pytest.mark.parametrize("encoded", ["?", 1, "YQ", {"unexpected": "value"}])
def test_invalid_signed_byte_encoding_holds(encoded):
    f = journal_fixture
    signed = b"signed"
    prepared = f._prepared(
        owner_candidate=f._owner_candidate(
            backend=f.SELF_MANAGED_BACKEND, evidence_kind=f.OwnerEvidenceKind.VERIFIED_SIGNER
        )
    )
    submission = f._submission(
        backend=f.SELF_MANAGED_BACKEND,
        signed_bytes=signed,
        transaction_hash=f.canonical_payload_digest(signed),
        account_sequence=1,
        submission_token=None,
    )
    journal = CreateJournal().append(JournalState.PREPARED, prepared, recorded_at=f.NOW)
    journal = journal.append(JournalState.SUBMITTED, submission, recorded_at=f.NOW + 1)
    raw = json.loads(canonical_journal_bytes(journal))
    raw["entries"][1]["payload"]["signed_bytes"] = {"$bytes": encoded}
    with pytest.raises(RecoveryDecodeError):
        decode_create_journal(_canonical(raw))


def test_request_recovered_with_wrong_root_is_rejected():
    with pytest.raises(RecoveryDecodeError):
        decode_prepared_create(canonical_journal_bytes(admission_fixture._request()))


def test_recovery_cannot_rewrite_typed_duplicate_reservations():
    state = admission_fixture._proposal().proposed_state
    raw = json.loads(canonical_journal_bytes(state))
    raw["reservations"] *= 2
    with pytest.raises(RecoveryDecodeError):
        decode_admission_state(_canonical(raw))


def test_prepared_golden_bytes_decode_and_preserve_the_exact_old_digest():
    # Written with the pre-codec serializer; a future codec/serializer drift
    # must not silently reinterpret a durable prepared record.
    from pathlib import Path

    raw = (Path(__file__).parent / "fixtures" / "recovery-prepared-v1.json").read_bytes()
    prepared = decode_prepared_create(raw)
    assert prepared == journal_fixture._prepared()
    assert canonical_journal_digest(prepared) == (
        "66b32a04d89303df7a4d9003027cd49ed8622bbad3537413e911b4985e7550cc"
    )
