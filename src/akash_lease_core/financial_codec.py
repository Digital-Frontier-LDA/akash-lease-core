"""Strict additive financial DATA recovery, never proof or effect authority.

The legacy recovery registry and its encodings are deliberately unchanged.
Only the explicitly listed financial data roots can be decoded here; callers
must reauthenticate retained store/ACK/source and fresh principal evidence.
"""

from __future__ import annotations

import json
import types
from collections.abc import Callable
from dataclasses import fields
from enum import Enum
from typing import TypeVar, Union, cast, get_args, get_origin, get_type_hints

from . import operation_exposure as financial
from .chain_identity import DeploymentKey
from .create_journal import (
    BackendIdentity,
    LifecycleIdentity,
    OwnerCandidate,
    PreparedCreate,
    PreparedGroup,
    ProducerProvenance,
    canonical_journal_bytes,
)
from .creation_admission import AdmissionScope

MAX_FINANCIAL_RECOVERY_BYTES = 8 * 1024 * 1024
MAX_FINANCIAL_RECOVERY_DEPTH = 64
MAX_FINANCIAL_RECOVERY_NODES = 100_000
_ROOTS = (
    financial.NativeLiabilityVector,
    financial.ServicePrincipalBinding,
    financial.FundingActorRegistration,
    financial.OperationMaximumLiability,
    financial.FinancialEffectIntent,
    financial.FinancialOperationState,
    financial.FinancialCommitBinding,
)
_DATA = (
    *_ROOTS,
    financial.NativeLiability,
    financial.QualifiedUactBasis,
    financial.ServicePrincipalObservation,
    financial.FinancialEffectObservation,
    financial.FinancialEffectRecord,
    BackendIdentity,
    AdmissionScope,
    DeploymentKey,
    PreparedCreate,
    PreparedGroup,
    OwnerCandidate,
    ProducerProvenance,
    LifecycleIdentity,
)
_HINTS = {cls: get_type_hints(cls) for cls in _DATA}
_T = TypeVar("_T")


class FinancialDecodeError(ValueError):
    """Stored financial data is noncanonical, malformed or beyond bounds."""


def _unique(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise FinancialDecodeError("duplicate financial field")
        result[key] = value
    return result


def _constant(value: str) -> None:
    raise FinancialDecodeError("non-finite financial quantity")


def _shape(value: object, depth: int, remaining: list[int]) -> None:
    remaining[0] -= 1
    if depth > MAX_FINANCIAL_RECOVERY_DEPTH or remaining[0] < 0:
        raise FinancialDecodeError("financial recovery structure exceeds bounds")
    if type(value) is dict:
        for child in value.values():
            _shape(child, depth + 1, remaining)
    elif type(value) is list:
        for child in value:
            _shape(child, depth + 1, remaining)


def _typed(value: object, expected: object) -> object:
    origin = get_origin(expected)
    if origin in (Union, types.UnionType):
        for candidate in get_args(expected):
            try:
                return _typed(value, candidate)
            except FinancialDecodeError:
                continue
        raise FinancialDecodeError("financial union is invalid")
    if origin is tuple:
        item, repetition = get_args(expected)
        if type(value) is not list or repetition is not Ellipsis:
            raise FinancialDecodeError("financial tuple is invalid")
        return tuple(_typed(child, item) for child in value)
    if expected in (str, int, bool, type(None)):
        if type(value) is not expected:
            raise FinancialDecodeError("financial primitive is invalid")
        return value
    if isinstance(expected, type) and issubclass(expected, Enum):
        if type(value) is not str:
            raise FinancialDecodeError("financial enum is invalid")
        try:
            return expected(value)
        except ValueError as exc:
            raise FinancialDecodeError("financial enum is invalid") from exc
    if expected not in _DATA or type(value) is not dict:
        raise FinancialDecodeError("financial type is not allowed")
    if value.get("$type") != expected.__name__:
        raise FinancialDecodeError("financial type tag disagrees")
    names = {field.name for field in fields(expected)}
    if set(value) != names | {"$type"}:
        raise FinancialDecodeError("financial fields are missing or unknown")
    try:
        constructor = cast(Callable[..., object], expected)
        return constructor(**{name: _typed(value[name], _HINTS[expected][name]) for name in names})
    except (ValueError, TypeError) as exc:
        raise FinancialDecodeError("financial model is invalid") from exc


def encode_financial_data(value: object) -> bytes:
    """Canonical data encoding; not an authenticated proof or authority token."""

    if type(value) not in _ROOTS:
        raise FinancialDecodeError("financial root type is not allowed")
    raw = canonical_journal_bytes(value)
    if len(raw) > MAX_FINANCIAL_RECOVERY_BYTES:
        raise FinancialDecodeError("financial recovery bytes exceed bounds")
    decode_financial_data(raw, type(value))
    return raw


def decode_financial_data(payload: bytes, expected: type[_T]) -> _T:
    """Decode a fixed data root. Decoding never activates a quote or attempt."""

    if expected not in _ROOTS:
        raise FinancialDecodeError("financial root type is not allowed")
    if type(payload) is not bytes or not 0 < len(payload) <= MAX_FINANCIAL_RECOVERY_BYTES:
        raise FinancialDecodeError("financial recovery bytes exceed bounds")
    try:
        value = json.loads(
            payload.decode("utf-8"), object_pairs_hook=_unique, parse_constant=_constant
        )
        _shape(value, 0, [MAX_FINANCIAL_RECOVERY_NODES])
        result = _typed(value, expected)
        if canonical_journal_bytes(result) != payload:
            raise FinancialDecodeError("financial encoding is not canonical")
    except (ValueError, TypeError, AttributeError, RecursionError):
        raise FinancialDecodeError("financial recovery data is invalid") from None
    return cast(_T, result)
