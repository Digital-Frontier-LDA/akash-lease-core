"""Canonical, typed identity for one Akash deployment."""

from __future__ import annotations

import re
from dataclasses import dataclass

__all__ = ["DeploymentKey", "is_canonical_akash_dseq", "is_canonical_akash_owner"]

_BECH32_ALPHABET = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"
_BECH32_VALUES = {character: index for index, character in enumerate(_BECH32_ALPHABET)}
_DSEQ_RE = re.compile(r"^[1-9][0-9]{0,19}$")
_DSEQ_MAX = 2**64 - 1


def _polymod(values: list[int]) -> int:
    generators = (0x3B6A57B2, 0x26508E6D, 0x1EA119FA, 0x3D4233DD, 0x2A1462B3)
    checksum = 1
    for value in values:
        top = checksum >> 25
        checksum = ((checksum & 0x1FFFFFF) << 5) ^ value
        for index, generator in enumerate(generators):
            if (top >> index) & 1:
                checksum ^= generator
    return checksum


def _hrp_expand(hrp: str) -> list[int]:
    return (
        [ord(character) >> 5 for character in hrp]
        + [0]
        + [ord(character) & 31 for character in hrp]
    )


def _convert_five_to_eight(values: list[int]) -> bytes | None:
    accumulator = 0
    bits = 0
    output = bytearray()
    for value in values:
        if value < 0 or value > 31:
            return None
        accumulator = (accumulator << 5) | value
        bits += 5
        while bits >= 8:
            bits -= 8
            output.append((accumulator >> bits) & 0xFF)
    if bits >= 5 or ((accumulator << (8 - bits)) & 0xFF):
        return None
    return bytes(output)


def is_canonical_akash_owner(value: object) -> bool:
    """Return whether *value* is a canonical 20-byte ``akash`` Bech32 account."""

    if not isinstance(value, str) or value != value.lower() or len(value) > 90:
        return False
    separator = value.rfind("1")
    if separator <= 0 or separator + 7 > len(value):
        return False
    hrp = value[:separator]
    if hrp != "akash":
        return False
    try:
        data = [_BECH32_VALUES[character] for character in value[separator + 1 :]]
    except KeyError:
        return False
    if _polymod(_hrp_expand(hrp) + data) != 1:
        return False
    payload = _convert_five_to_eight(data[:-6])
    return payload is not None and len(payload) == 20


def is_canonical_akash_dseq(value: object) -> bool:
    """Return whether *value* is a canonical positive Akash uint64 string."""

    return (
        isinstance(value, str)
        and _DSEQ_RE.fullmatch(value) is not None
        and int(value) <= _DSEQ_MAX
    )


@dataclass(frozen=True)
class DeploymentKey:
    """The indivisible owner and DSEQ identity required for chain operations."""

    owner: str
    dseq: str

    def __post_init__(self) -> None:
        if not is_canonical_akash_owner(self.owner):
            raise ValueError("owner must be a canonical 20-byte akash Bech32 account")
        if not is_canonical_akash_dseq(self.dseq):
            raise ValueError("dseq must be a canonical positive Akash uint64 string")
