"""Strict recovery of canonical stored values; decoding never issues authority.

Only data reachable from the four recovery roots is admitted. In particular,
permits, proposals and submission authorizations are intentionally absent. The
broker must authenticate its store, compare revisions and obtain fresh evidence
before using recovered data. This module does none of those I/O operations.
"""

from __future__ import annotations

import base64
import binascii
import json
import types
from dataclasses import fields
from enum import Enum
from typing import TypeVar, Union, get_args, get_origin, get_type_hints

from . import create_journal as journal
from . import creation_admission as admission
from .chain_identity import DeploymentKey

MAX_RECOVERY_BYTES = 8 * 1024 * 1024
MAX_RECOVERY_DEPTH = 64
MAX_RECOVERY_NODES = 100_000

# This is a versioned storage-data allowlist, not a lookup of caller-named Python
# objects. New model types require deliberate codec and compatibility review.
_DATA_TYPES = (
    DeploymentKey,
    journal.BackendIdentity,
    journal.ProducerProvenance,
    journal.LifecycleIdentity,
    journal.OwnerCandidate,
    journal.PreparedGroup,
    journal.PreparedCreate,
    journal.SubmissionEvidence,
    journal.RejectionEvidence,
    journal.NonCommitEvidence,
    journal.UncertaintyEvidence,
    journal.TransactionEventEvidence,
    journal.ExactChainReadEvidence,
    journal.BoundDeployment,
    journal.CreateOutcome,
    journal.HandoffFailure,
    journal.CreatorRollbackRequest,
    journal.ExecutionClosure,
    journal.SettlementEvidence,
    journal.JournalEntry,
    journal.CreateJournal,
    admission.AdmissionScope,
    admission.OwnerBudgetScope,
    admission.ContainmentLimits,
    admission.ContainmentCensus,
    admission.ScopeBudget,
    admission.CreateExposureEvidence,
    admission.AdmissionRequest,
    admission.CapacityReservation,
    admission.AdmissionState,
)
_TYPES = {cls.__name__: cls for cls in _DATA_TYPES}
_HINTS = {cls: get_type_hints(cls) for cls in _DATA_TYPES}
_T = TypeVar("_T")


class RecoveryDecodeError(ValueError):
    """Stored bytes are not a canonical, valid value of the requested root."""


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise RecoveryDecodeError("duplicate recovery field")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise RecoveryDecodeError("non-finite recovery number")


def _check_shape(value: object, depth: int, remaining: list[int]) -> None:
    remaining[0] -= 1
    if depth > MAX_RECOVERY_DEPTH or remaining[0] < 0:
        raise RecoveryDecodeError("recovery structure exceeds bounds")
    if type(value) is dict:
        for child in value.values():
            _check_shape(child, depth + 1, remaining)
    elif type(value) is list:
        for child in value:
            _check_shape(child, depth + 1, remaining)


def _typed(value: object, expected: object) -> object:
    origin = get_origin(expected)
    if origin in (Union, types.UnionType):
        for candidate in get_args(expected):
            try:
                return _typed(value, candidate)
            except RecoveryDecodeError:
                continue
        raise RecoveryDecodeError("recovery union member is invalid")
    if origin is tuple:
        item_type, repetition = get_args(expected)
        if type(value) is not list or repetition is not Ellipsis:
            raise RecoveryDecodeError("recovery tuple is invalid")
        return tuple(_typed(item, item_type) for item in value)
    if expected in (str, int, bool, type(None)):
        if type(value) is not expected:
            raise RecoveryDecodeError("recovery primitive type is invalid")
        return value
    if expected is bytes:
        if type(value) is not dict or set(value) != {"$bytes"}:
            raise RecoveryDecodeError("recovery bytes are invalid")
        try:
            return base64.b64decode(value["$bytes"], validate=True)
        except (ValueError, TypeError, binascii.Error) as exc:
            raise RecoveryDecodeError("recovery bytes are invalid") from exc
    if isinstance(expected, type) and issubclass(expected, Enum):
        if type(value) is not str:
            raise RecoveryDecodeError("recovery enum is invalid")
        try:
            return expected(value)
        except ValueError as exc:
            raise RecoveryDecodeError("recovery enum is invalid") from exc
    if expected not in _DATA_TYPES or type(value) is not dict:
        raise RecoveryDecodeError("recovery data type is not allowed")
    if _TYPES.get(value.get("$type")) is not expected:
        raise RecoveryDecodeError("recovery type tag disagrees with field")
    names = {field.name for field in fields(expected)}
    if set(value) != names | {"$type"}:
        raise RecoveryDecodeError("recovery fields are incomplete or unknown")
    try:
        return expected(**{name: _typed(value[name], _HINTS[expected][name]) for name in names})
    except (ValueError, TypeError) as exc:
        raise RecoveryDecodeError("recovery model validation failed") from exc


def _decode(payload: bytes, expected: type[_T]) -> _T:
    if type(payload) is not bytes or not 0 < len(payload) <= MAX_RECOVERY_BYTES:
        raise RecoveryDecodeError("recovery bytes exceed bounds or have wrong type")
    try:
        value = json.loads(
            payload.decode("utf-8"),
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
        _check_shape(value, 0, [MAX_RECOVERY_NODES])
        result = _typed(value, expected)
        if journal.canonical_journal_bytes(result) != payload:
            raise RecoveryDecodeError("recovery encoding is not canonical")
    except (ValueError, TypeError, RecursionError):
        # Do not reflect stored credentials, signed transaction bytes or model
        # constructor details into a caller's logs/error response.
        raise RecoveryDecodeError("invalid canonical recovery value") from None
    return result


def decode_create_journal(payload: bytes) -> journal.CreateJournal:
    """Revalidate every entry, hash link and lifecycle transition after restart."""
    return _decode(payload, journal.CreateJournal)


def decode_admission_state(payload: bytes) -> admission.AdmissionState:
    """Recover reservations without issuing or redeeming a create permit."""
    return _decode(payload, admission.AdmissionState)


def decode_prepared_create(payload: bytes) -> journal.PreparedCreate:
    """Recover prepared identity and exact request/group digests."""
    return _decode(payload, journal.PreparedCreate)


def decode_admission_request(payload: bytes) -> admission.AdmissionRequest:
    """Recover a bound request for reconciliation, not network resubmission."""
    return _decode(payload, admission.AdmissionRequest)
