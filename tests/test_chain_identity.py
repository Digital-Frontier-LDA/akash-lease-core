import pytest

from akash_lease_core import (
    DeploymentKey,
    is_canonical_akash_dseq,
    is_canonical_akash_owner,
)

VALID_OWNER = "akash1n4uut3vxmkdp8wsrya3q0qyddgqey0rh9as4ee"


def test_canonical_owner_accepts_a_checksum_valid_20_byte_account():
    assert is_canonical_akash_owner(VALID_OWNER)


@pytest.mark.parametrize(
    "value",
    [
        None,
        1,
        "",
        VALID_OWNER.upper(),
        "cosmos" + VALID_OWNER[5:],
        VALID_OWNER[:-1] + ("q" if VALID_OWNER[-1] != "q" else "p"),
        "akash1qqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqmcn030",
    ],
)
def test_owner_rejects_wrong_type_case_hrp_checksum_and_payload_length(value):
    assert not is_canonical_akash_owner(value)


@pytest.mark.parametrize("value", ["1", "18446744073709551615"])
def test_canonical_dseq_accepts_positive_uint64_boundaries(value):
    assert is_canonical_akash_dseq(value)


@pytest.mark.parametrize(
    "value",
    [None, 1, "", "0", "01", "+1", "1 ", "١", "18446744073709551616", "9" * 200],
)
def test_dseq_rejects_noncanonical_and_out_of_range_values(value):
    assert not is_canonical_akash_dseq(value)


def test_deployment_key_keeps_owner_and_dseq_indivisible():
    assert DeploymentKey(VALID_OWNER, "42") == DeploymentKey(owner=VALID_OWNER, dseq="42")


@pytest.mark.parametrize(
    ("owner", "dseq"),
    [(VALID_OWNER[:-1] + "q", "42"), (VALID_OWNER, "0")],
)
def test_deployment_key_refuses_if_either_half_is_invalid(owner, dseq):
    with pytest.raises(ValueError):
        DeploymentKey(owner, dseq)
