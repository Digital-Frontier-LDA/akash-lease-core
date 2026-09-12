#!/usr/bin/env python3
"""Build or verify the immutable-ready v1 interoperability artifact bundle."""

from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
import sys
import types
import typing
from enum import Enum
from pathlib import Path

from akash_lease_core import (
    EMPTY_RESERVATION_POPULATION_DIGEST,
    AdmissionRequest,
    AdmissionScope,
    AdmissionState,
    AuthenticatedBrokerEvidence,
    BackendIdentity,
    BackendKind,
    CensusStatus,
    ContainmentCensus,
    ContainmentLimits,
    CreateExposureEvidence,
    LifecycleIdentity,
    OwnerBudgetScope,
    OwnerCandidate,
    OwnerEvidenceKind,
    PersistenceConfirmation,
    PreparedCreate,
    PreparedGroup,
    ProducerProvenance,
    ScopeBudget,
    canonical_admission_state_digest,
    canonical_group_population_digest,
    canonical_journal_digest,
    confirm_persisted_reservation,
    reserve_capacity,
)
from akash_lease_core import create_journal as journal
from akash_lease_core import creation_admission as admission

ROOT = Path(__file__).resolve().parents[1]
BUNDLE = ROOT / "interoperability" / "akash-lifecycle-interoperability-v1"
NOW = 1_800_000_000
OWNER = "akash1n4uut3vxmkdp8wsrya3q0qyddgqey0rh9as4ee"
POLICY_REVISION = "9" * 40
SCHEMA_VERSION = "akash-lifecycle-interoperability/v1"


def canonical_bytes(value: object) -> bytes:
    """Canonical JSON profile shared by the two fixture adapters."""

    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()


def digest(value: object) -> str:
    return "sha256:" + hashlib.sha256(canonical_bytes(value)).hexdigest()


def typed(value: object) -> object:
    """Expose the core's existing canonical typed-record representation."""

    return json.loads(journal.canonical_journal_bytes(value))


def scope() -> AdmissionScope:
    return AdmissionScope(
        chain_id="akashnet-2",
        owner=OWNER,
        producer_issuer="https://token.actions.githubusercontent.com",
        repository_owner_id="456",
        repository_id="123",
        repository="example/repo",
        workload_class="ci-runner",
    )


def prepared(operation_id: str = "create:example/repo:42:1") -> PreparedCreate:
    current_scope = scope()
    groups = (PreparedGroup(1, "example-idv1-class-ci-runner-g1-attempt-1-run-42-end"),)
    return PreparedCreate(
        operation_id=operation_id,
        operation_ordinal=1,
        producer=ProducerProvenance(
            repository=current_scope.repository,
            repository_id=current_scope.repository_id,
            repository_owner_id=current_scope.repository_owner_id,
            issuer=current_scope.producer_issuer,
            audience="akash-create",
            subject="repo:example/repo:ref:refs/heads/main",
            workflow_ref="example/repo/.github/workflows/deploy.yml@refs/heads/main",
            workflow_sha="1" * 40,
            ref="refs/heads/main",
            jti="oidc-jti-1",
            authentication_evidence_digest="2" * 64,
        ),
        lifecycle=LifecycleIdentity(
            repository=current_scope.repository,
            workload_class=current_scope.workload_class,
            run=42,
            run_attempt=1,
        ),
        owner_candidate=OwnerCandidate(
            backend=BackendIdentity(BackendKind.MEDIATED, "console:production"),
            owner=current_scope.owner,
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


def request(operation_id: str = "create:example/repo:42:1") -> AdmissionRequest:
    current_scope = scope()
    operation = prepared(operation_id)
    exposure = CreateExposureEvidence(
        chain_id=current_scope.chain_id,
        operation_id=operation.operation_id,
        prepared_operation_digest=canonical_journal_digest(operation),
        request_digest=operation.request_digest,
        sdl_digest=operation.sdl_digest,
        backend=operation.owner_candidate.backend,
        backend_policy_revision=POLICY_REVISION,
        amount_uact=100,
        source="signed backend exposure policy",
        evidence_digest="7" * 64,
        observed_at=NOW - 1,
        valid_until=NOW + 20,
    )
    return AdmissionRequest(operation, current_scope, exposure, NOW)


def limits(*, max_active: int = 3) -> ContainmentLimits:
    return ContainmentLimits(
        max_active_deployments=max_active,
        max_unresolved_create_outcomes=2,
        max_financial_exposure_uact=500,
        exposure_policy_revision=POLICY_REVISION,
        admission_policy_revision=POLICY_REVISION,
        policy_reference="policy:2026-09-12",
        valid_until=NOW + 100,
    )


def census(*, active: int = 0) -> ContainmentCensus:
    return ContainmentCensus(
        active_deployments=active,
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


def state(*, active: int = 0, max_active: int = 3) -> AdmissionState:
    current_scope = scope()
    budgets = tuple(
        ScopeBudget(item, limits(max_active=max_active), census(active=active))
        for item in (OwnerBudgetScope(current_scope.chain_id, current_scope.owner), current_scope)
    )
    return AdmissionState(budgets)


def confirmation(proposal, *, persisted_revision=None, persisted_digest=None):
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
    return PersistenceConfirmation(
        proposal.operation_id,
        proposal.proposed_revision if persisted_revision is None else persisted_revision,
        proposal.proposed_state_digest if persisted_digest is None else persisted_digest,
        broker,
        "b" * 64,
        NOW + 1,
    )


def _result(disposition: str, reason: str, proposal_revision: int | None) -> dict[str, object]:
    return {
        "disposition": disposition,
        "reason": reason,
        "proposal_revision": proposal_revision,
    }


def _confirmation_result(proposal, pre_state, current_request, observed_state, receipt):
    try:
        confirm_persisted_reservation(
            proposal, pre_state, current_request, observed_state, receipt
        )
    except (admission.PersistenceConfirmationError, ValueError) as exc:
        return {
            "accepted": False,
            "core_exception": type(exc).__name__,
            "permit_issued": False,
        }
    return {"accepted": True, "core_exception": None, "permit_issued": True}


def _forge(value, **changes):
    forged = object.__new__(type(value))
    for field in dataclasses.fields(value):
        object.__setattr__(forged, field.name, changes.get(field.name, getattr(value, field.name)))
    return forged


def build_vectors() -> dict[str, object]:
    initial = state()
    admission_request = request()
    admitted = reserve_capacity(initial, admission_request, expected_revision=0, now=NOW)
    if admitted.proposal is None:
        raise AssertionError("reference admission did not produce a reservation proposal")

    largest_exact_js_integer = 2**53 - 1
    rejected_state = state(active=largest_exact_js_integer, max_active=largest_exact_js_integer)
    rejected = reserve_capacity(rejected_state, admission_request, expected_revision=0, now=NOW)
    replay = reserve_capacity(
        admitted.proposal.proposed_state,
        admission_request,
        expected_revision=admitted.proposal.proposed_revision,
        now=NOW,
    )
    mismatched_confirmation = confirmation(
        admitted.proposal,
        persisted_revision=initial.revision,
        persisted_digest=canonical_admission_state_digest(initial),
    )
    valid_confirmation = confirmation(admitted.proposal)
    confirmation_cases = (
        (
            "confirmation-success-issues-permit",
            initial,
            admission_request,
            admitted.proposal.proposed_state,
            valid_confirmation,
        ),
        (
            "partial-store-refuses-permit",
            initial,
            admission_request,
            initial,
            mismatched_confirmation,
        ),
        (
            "foreign-request-refuses-permit",
            initial,
            request("create:example/repo:42:2"),
            admitted.proposal.proposed_state,
            valid_confirmation,
        ),
        (
            "expired-broker-evidence-refuses-permit",
            initial,
            admission_request,
            admitted.proposal.proposed_state,
            dataclasses.replace(
                valid_confirmation,
                authenticated_broker=dataclasses.replace(
                    valid_confirmation.authenticated_broker,
                    valid_until=valid_confirmation.persisted_at - 1,
                ),
            ),
        ),
        (
            "wrong-persisted-digest-refuses-permit",
            initial,
            admission_request,
            admitted.proposal.proposed_state,
            dataclasses.replace(valid_confirmation, persisted_state_digest="d" * 64),
        ),
        (
            "foreign-operation-confirmation-refuses-permit",
            initial,
            admission_request,
            admitted.proposal.proposed_state,
            dataclasses.replace(valid_confirmation, operation_id="other-operation"),
        ),
        (
            "foreign-pre-state-refuses-permit",
            state(active=1),
            admission_request,
            admitted.proposal.proposed_state,
            valid_confirmation,
        ),
        (
            "malformed-persistence-evidence-digest-refuses-permit",
            initial,
            admission_request,
            admitted.proposal.proposed_state,
            _forge(valid_confirmation, evidence_digest="not-a-digest"),
        ),
    )

    vectors = [
        {
            "id": "reservation-success",
            "operation": "reserve_capacity",
            "input": {
                "state": typed(initial),
                "request": typed(admission_request),
                "expected_revision": 0,
                "now": NOW,
            },
            "expected": _result(
                admitted.decision.disposition.value,
                admitted.decision.reason.value,
                admitted.proposal.proposed_revision,
            ),
        },
        {
            "id": "reservation-rejected-at-active-limit",
            "operation": "reserve_capacity",
            "input": {
                "state": typed(rejected_state),
                "request": typed(admission_request),
                "expected_revision": 0,
                "now": NOW,
            },
            "expected": _result(
                rejected.decision.disposition.value, rejected.decision.reason.value, None
            ),
        },
        {
            "id": "reservation-replay-reconciles",
            "operation": "reserve_capacity",
            "input": {
                "state": typed(admitted.proposal.proposed_state),
                "request": typed(admission_request),
                "expected_revision": admitted.proposal.proposed_revision,
                "now": NOW,
            },
            "expected": _result(
                replay.decision.disposition.value, replay.decision.reason.value, None
            ),
        },
    ]
    for case_id, pre_state, current_request, observed_state, receipt in confirmation_cases:
        vectors.append(
            {
                "id": case_id,
                "operation": "confirm_persisted_reservation",
                "input": {
                    "proposal": typed(admitted.proposal),
                    "pre_state": typed(pre_state),
                    "request": typed(current_request),
                    "observed_state": typed(observed_state),
                    "confirmation": typed(receipt),
                },
                "expected": _confirmation_result(
                    admitted.proposal,
                    pre_state,
                    current_request,
                    observed_state,
                    receipt,
                ),
            }
        )

    canonical_entries = (
        ("𐀀", "astral-key"),
        ("\ue000", "bmp-private-use-key"),
        ("z", "\u0000\b\f\n\r\t"),
        ("a", "rocket: 🚀"),
    )
    canonical_value = dict(canonical_entries)
    vectors.append(
        {
            "id": "canonical-json-control-astral-and-key-order",
            "operation": "canonical_json_entries",
            "input": {"entries": canonical_entries},
            "expected": {"canonical_json": canonical_bytes(canonical_value).decode("ascii")},
        }
    )
    for decimal_text in (
        str(2**53 - 1),
        str(2**53),
        str(2**64 - 1),
        str(10**40 + 123),
    ):
        vectors.append(
            {
                "id": f"canonical-nonnegative-integer-{decimal_text}",
                "operation": "canonical_nonnegative_integer",
                "input": {"decimal_text": decimal_text, "value": int(decimal_text)},
                "expected": {"accepted": True, "canonical_json": decimal_text},
            }
        )
    for decimal_text in ("01", "-1"):
        vectors.append(
            {
                "id": f"reject-noncanonical-integer-{decimal_text.replace('-', 'minus')}",
                "operation": "canonical_nonnegative_integer",
                "input": {"decimal_text": decimal_text},
                "expected": {"accepted": False, "canonical_json": None},
            }
        )

    for vector in vectors:
        payload = {"input": vector["input"], "expected": vector["expected"]}
        vector["canonical_payload_sha256"] = digest(payload)
        vector["canonical_payload_utf8_hex"] = canonical_bytes(payload).hex()
    return {"contract": SCHEMA_VERSION, "vectors": vectors}


MODEL_TYPES = (
    journal.DeploymentKey,
    journal.BackendIdentity,
    journal.ProducerProvenance,
    journal.LifecycleIdentity,
    journal.OwnerCandidate,
    journal.PreparedGroup,
    journal.PreparedCreate,
    journal.TransactionEventEvidence,
    journal.ExactChainReadEvidence,
    journal.BoundDeployment,
    admission.AdmissionScope,
    admission.OwnerBudgetScope,
    admission.ContainmentLimits,
    admission.ContainmentCensus,
    admission.ScopeBudget,
    admission.CreateExposureEvidence,
    admission.AdmissionRequest,
    admission.CapacityReservation,
    admission.AdmissionState,
    admission.CapacityUsage,
    admission.BudgetProjection,
    admission.AdmissionDecision,
    admission.ReservationProposal,
    admission.AdmissionResult,
    admission.AuthenticatedBrokerEvidence,
    admission.PersistenceConfirmation,
    admission.AdmissionPolicyBinding,
    admission.PermitPresentation,
    admission.CreatePermit,
    admission.PermitRedemptionProposal,
    admission.CreateSubmissionAuthorization,
)

MODEL_ENUMS = (
    journal.BackendKind,
    journal.OwnerEvidenceKind,
    admission.CensusStatus,
    admission.ReservationState,
    admission.AdmissionDisposition,
    admission.AdmissionReason,
    admission.PermitRevocationStatus,
)


def type_expression(annotation: object) -> object:
    origin = typing.get_origin(annotation)
    arguments = typing.get_args(annotation)
    if annotation in {str, int, bool}:
        return annotation.__name__
    if annotation is type(None):
        return "null"
    if origin is tuple:
        if len(arguments) != 2 or arguments[1] is not Ellipsis:
            raise TypeError(f"only homogeneous tuples are supported, got {annotation!r}")
        return {"sequence": type_expression(arguments[0])}
    if origin in {typing.Union, types.UnionType}:
        return {"one_of": [type_expression(item) for item in arguments]}
    if isinstance(annotation, type) and issubclass(annotation, (Enum,)):
        return {"enum": annotation.__name__}
    if isinstance(annotation, type) and dataclasses.is_dataclass(annotation):
        return {"record": annotation.__name__}
    raise TypeError(f"unsupported model annotation {annotation!r}")


def build_model() -> dict[str, object]:
    records = {}
    for model_type in MODEL_TYPES:
        hints = typing.get_type_hints(model_type)
        records[model_type.__name__] = {
            "fields": [
                {
                    "name": field.name,
                    "type": type_expression(hints[field.name]),
                    # canonical_journal_bytes emits every dataclass field. A Python
                    # constructor default is not permission to omit a wire field.
                    "required": True,
                }
                for field in dataclasses.fields(model_type)
            ],
            "canonical_tag": model_type.__name__,
            "closed": True,
        }
    model = {
        "contract": SCHEMA_VERSION,
        "wire_roots": [
            "AdmissionRequest",
            "AdmissionResult",
            "PersistenceConfirmation",
            "CreatePermit",
            "PermitPresentation",
            "PermitRedemptionProposal",
            "CreateSubmissionAuthorization",
        ],
        "storage_roots": ["AdmissionState", "ReservationProposal", "PermitRedemptionProposal"],
        "enums": {item.__name__: [member.value for member in item] for item in MODEL_ENUMS},
        "records": records,
    }
    referenced_records: set[str] = set()

    def collect_references(value: object) -> None:
        if isinstance(value, dict):
            if "record" in value:
                referenced_records.add(typing.cast(str, value["record"]))
            for item in value.values():
                collect_references(item)
        elif isinstance(value, list):
            for item in value:
                collect_references(item)

    collect_references(records)
    missing = referenced_records - records.keys()
    if missing:
        raise AssertionError(f"machine schema has dangling record references: {sorted(missing)}")
    return model


def canonicalization() -> dict[str, object]:
    return {
        "contract": SCHEMA_VERSION,
        "encoding": "UTF-8",
        "json_profile": {
            "object_key_order": "ascending Unicode code point",
            "object_keys": "Unicode scalar strings; typed record field names are ASCII",
            "strings": "Unicode scalar strings only; lone surrogate code points rejected",
            "whitespace": "none outside strings",
            "string_escaping": "JSON with every non-ASCII code point escaped",
            "numbers": "base-10 integers only; no leading zero; arbitrary precision",
            "duplicate_object_keys": "reject",
            "non_finite_numbers": "reject",
            "terminal_newline": False,
        },
        "typed_values": {
            "record_tag": "$type",
            "bytes": {"shape": {"$bytes": "RFC 4648 canonical base64 with padding"}},
            "tuple": "JSON array preserving source tuple order",
            "enum": "JSON string equal to the declared enum value",
        },
    }


def coverage() -> dict[str, object]:
    roles = {
        "broker.admission_request": {
            "status": "supported",
            "shape": "AdmissionRequest",
        },
        "broker.admission_result": {
            "status": "supported",
            "shape": "AdmissionResult",
        },
        "broker.typed_error": {
            "status": "unavailable_fail_closed",
            "reason": "v0.12 raises typed exceptions but defines no broker error envelope",
        },
        "broker.idempotency": {
            "status": "unavailable_fail_closed",
            "reason": "replay semantics exist, but v0.12 defines no broker idempotency wire",
        },
        "storage.prepared_create": {
            "status": "supported",
            "shape": "PreparedCreate nested in AdmissionRequest",
        },
        "storage.create_journal": {
            "status": "unavailable_fail_closed",
            "reason": "v0.12 has a journal model but this bundle does not catalog its store wire",
        },
        "storage.allocation": {
            "status": "unavailable_fail_closed",
            "reason": "v0.12 defines no durable broker allocation record",
        },
        "storage.capacity_reservation": {
            "status": "supported",
            "shape": "AdmissionState and ReservationProposal",
        },
        "storage.permit_issuance": {
            "status": "supported",
            "shape": "PersistenceConfirmation and CreatePermit",
        },
        "storage.permit_redemption": {
            "status": "supported",
            "shape": "PermitRedemptionProposal and CreateSubmissionAuthorization",
        },
        "storage.record_outcome": {
            "status": "unavailable_fail_closed",
            "reason": (
                "v0.12 has a transition function but this bundle defines no outcome store wire"
            ),
        },
        "storage.reconciliation": {
            "status": "unavailable_fail_closed",
            "reason": "replay returns reconcile, but v0.12 defines no reconciliation store wire",
        },
        "storage.execution_close": {
            "status": "unavailable_fail_closed",
            "reason": "v0.12 has typed evidence but no broker close wire",
        },
        "storage.financial_settlement": {
            "status": "unavailable_fail_closed",
            "reason": "v0.12 has typed evidence but no broker settlement wire",
        },
        "transaction.atomic_membership": {
            "status": "unavailable_fail_closed",
            "reason": (
                "v0.12 does not define the broker atomic journal/allocation/reservation/permit set"
            ),
        },
        "chain.finality_observation": {
            "status": "unavailable_fail_closed",
            "reason": "v0.12 has typed evidence but no broker finality observation wire",
        },
        "runtime.io_adapter": {
            "status": "unavailable_fail_closed",
            "reason": "artifact bundle only; no I/O or persistence adapter is supplied",
        },
    }
    return {
        "contract": SCHEMA_VERSION,
        "normative_role_count": len(roles),
        "normative_role_population": sorted(roles),
        "roles": roles,
        "measured_outcomes": [
            "proposed",
            "active_deployment_limit",
            "reconcile_existing",
            "PersistenceConfirmationError",
        ],
    }


def artifacts() -> dict[str, object]:
    return {
        "canonicalization.json": canonicalization(),
        "model.json": build_model(),
        "vectors.json": build_vectors(),
        "coverage.json": coverage(),
    }


def manifest(payloads: dict[str, object]) -> dict[str, object]:
    kinds = {
        "canonicalization.json": "canonicalization-rules",
        "model.json": "machine-schema",
        "vectors.json": "golden-vectors",
        "coverage.json": "coverage-declaration",
    }
    return {
        "contract": SCHEMA_VERSION,
        "bundle_format": 1,
        "core_model": {"package": "akash-lease-core", "version": "0.12.0"},
        "artifacts": [
            {
                "kind": kinds[name],
                "id": f"{SCHEMA_VERSION}/{name.removesuffix('.json')}",
                "media_type": "application/json",
                "path": name,
                "digest": digest(payload),
            }
            for name, payload in sorted(payloads.items())
        ],
    }


def write_or_check(*, check: bool) -> int:
    payloads = artifacts()
    payloads["bundle.json"] = manifest(payloads)
    differences = []
    for name, payload in payloads.items():
        expected = canonical_bytes(payload)
        path = BUNDLE / name
        if check:
            if not path.exists() or path.read_bytes() != expected:
                differences.append(str(path.relative_to(ROOT)))
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(expected)
    if differences:
        print("interoperability bundle is stale: " + ", ".join(differences), file=sys.stderr)
        return 1
    action = "verified" if check else "wrote"
    print(f"{action} {len(payloads)} bundle artifacts")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    return write_or_check(check=parser.parse_args().check)


if __name__ == "__main__":
    raise SystemExit(main())
