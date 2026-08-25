"""`from_provider_status` — the adapter that makes EMPTIEST feedable.

⛔ WHY THIS FILE EXISTS AT ALL. `ProviderCapacity` and `PreferredSelection.EMPTIEST`
shipped and stayed dormant because nothing could populate them, and the reported
reason — "providers do not publish a host_uri" — was measured against two provider
addresses that had been mis-transcribed. A wrong identifier and an absent field
return the same empty body. Re-measured against the addresses in the registry:
16 of 16 vetted providers publish a host_uri and 14 of 16 serve a readable /status.

The fixture here is CAPTURED FROM A LIVE PROVIDER, not hand-written, because a
paraphrased fixture tests the paraphrase.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from akash_lease_core import ProviderCapacity, from_provider_status

FIXTURE = Path(__file__).parent / "fixtures" / "provider_status_sofia.json"


@pytest.fixture()
def live_status() -> dict:
    return json.loads(FIXTURE.read_text())


def test_parses_a_real_provider_payload(live_status: dict) -> None:
    cap = from_provider_status(live_status)
    assert cap.is_readable
    frac = cap.available_fraction()
    assert 0.0 <= frac <= 1.0


def test_the_binding_fraction_is_the_MINIMUM_not_the_mean(live_status: dict) -> None:
    """⭐ A provider 90% free on memory and 2% free on CPU cannot take a CPU-bound
    workload. The mean would recommend exactly the provider that refuses the bid."""
    cap = from_provider_status(live_status)
    readable = [v for v in (cap.cpu, cap.memory, cap.storage, cap.gpu) if v is not None]
    assert cap.available_fraction() == min(readable)


def test_gpu_is_not_applicable_rather_than_full(live_status: dict) -> None:
    """A CPU-only provider reports 0 allocatable GPUs. That must be None, never 0.0 —
    0.0 would drive the binding minimum to zero and rank a healthy provider as full."""
    cap = from_provider_status(live_status)
    assert cap.gpu is None
    assert cap.available_fraction() > 0.0


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param(None, id="not-a-mapping"),
        pytest.param({}, id="empty"),
        pytest.param({"cluster": {}}, id="no-inventory"),
        pytest.param({"cluster": {"inventory": {}}}, id="no-available"),
        pytest.param({"cluster": {"inventory": {"available": {}}}}, id="no-nodes"),
        pytest.param({"cluster": {"inventory": {"available": {"nodes": []}}}}, id="empty-nodes"),
        pytest.param(
            {"cluster": {"inventory": {"available": {"nodes": [{"name": "n"}]}}}},
            id="node-without-numbers",
        ),
        pytest.param(
            {"cluster": {"inventory": {"available": {"nodes": "x"}}}}, id="nodes-not-a-list"
        ),
    ],
)
def test_an_unreadable_payload_is_UNREADABLE_never_full(payload: object) -> None:
    """⛔ THE LOAD-BEARING TEST. Every failure path must yield None, never 0.0.

    None means 'do not rank this provider'. 0.0 means 'measured, and completely
    full', which sorts it last on evidence the parser does not have — so a provider
    behind a flaky endpoint would be permanently deprioritised for being
    unreachable rather than for being busy.
    """
    cap = from_provider_status(payload)
    assert cap.available_fraction() is None
    assert not cap.is_readable
    assert cap == ProviderCapacity()


def test_a_malformed_payload_does_not_raise() -> None:
    """The caller owns I/O and its failures; the parser must not add a second
    failure mode the auction has to catch. `is_readable` is the channel."""
    from_provider_status({"cluster": {"inventory": {"available": {"nodes": [1, 2, 3]}}}})


def test_sums_across_nodes_rather_than_taking_the_first() -> None:
    status = {
        "cluster": {
            "inventory": {
                "available": {
                    "nodes": [
                        {
                            "allocatable": {
                                "cpu": 100,
                                "gpu": 0,
                                "memory": 100,
                                "storage_ephemeral": 100,
                            },
                            "available": {
                                "cpu": 10,
                                "gpu": 0,
                                "memory": 50,
                                "storage_ephemeral": 50,
                            },
                        },
                        {
                            "allocatable": {
                                "cpu": 100,
                                "gpu": 0,
                                "memory": 100,
                                "storage_ephemeral": 100,
                            },
                            "available": {
                                "cpu": 90,
                                "gpu": 0,
                                "memory": 50,
                                "storage_ephemeral": 50,
                            },
                        },
                    ]
                }
            }
        }
    }
    cap = from_provider_status(status)
    # 100 free of 200 total on cpu — NOT 10/100 from the first node alone.
    assert cap.cpu == pytest.approx(0.5)


def test_booleans_are_rejected_rather_than_counted_as_one() -> None:
    """⚠ bool is an int subclass. `True` would silently contribute 1 unit."""
    status = {
        "cluster": {
            "inventory": {
                "available": {
                    "nodes": [
                        {
                            "allocatable": {
                                "cpu": True,
                                "memory": 100,
                                "gpu": 0,
                                "storage_ephemeral": 100,
                            },
                            "available": {
                                "cpu": True,
                                "memory": 50,
                                "gpu": 0,
                                "storage_ephemeral": 50,
                            },
                        }
                    ]
                }
            }
        }
    }
    cap = from_provider_status(status)
    assert cap.cpu is None  # cpu was skipped entirely, not counted as 1/1 = 100% free
    assert cap.memory == pytest.approx(0.5)


def _one_node(allocatable: dict, available: dict) -> dict:
    return {
        "cluster": {
            "inventory": {
                "available": {"nodes": [{"allocatable": allocatable, "available": available}]}
            }
        }
    }


@pytest.mark.parametrize(
    ("allocatable", "available", "why"),
    [
        pytest.param({"cpu": 100}, {"cpu": True}, "free is a bool", id="free-bool"),
        pytest.param({"cpu": True}, {"cpu": 10}, "total is a bool", id="total-bool"),
        pytest.param({"cpu": 100}, {"cpu": "10"}, "free is a string", id="free-str"),
        pytest.param({"cpu": 100}, {}, "free is absent", id="free-absent"),
        pytest.param({"cpu": 100}, {"cpu": float("nan")}, "free is NaN", id="free-nan"),
        pytest.param({"cpu": 100}, {"cpu": -5}, "free is negative", id="free-negative"),
    ],
)
def test_a_HALF_valid_pair_is_dropped_whole_never_counted_as_zero_free(
    allocatable: dict, available: dict, why: str
) -> None:
    """⛔⛔ REGRESSION, CodeRabbit on #26. The pair is atomic.

    Guarding each value separately let a valid `total` through while the matching
    `free` was rejected, so the dimension became ``0 / 100 == 0.0`` — "measured,
    and completely full". That is the exact value this module exists never to emit
    from unreadable input, and under EMPTIEST it sorts a healthy provider last on
    evidence that is garbage.

    ⚠ The original bool test could not catch this: it set BOTH halves to ``True``,
    so the symmetric case passed and the asymmetric one was never asked.
    """
    cap = from_provider_status(_one_node(allocatable, available))
    assert cap.cpu is None, f"{why}: expected UNREADABLE, got {cap.cpu}"
    assert cap.available_fraction() is None


def test_an_aggregate_that_overflows_is_unreadable_not_an_exception() -> None:
    """⛔ REGRESSION, CodeRabbit on #26.

    Each per-node value is finite, but their sum is not. `from_totals()` raises
    ValueError on a non-finite total, which would break this module's documented
    promise that a malformed payload yields an unreadable capacity rather than an
    exception the caller must catch.
    """
    huge = 1e308
    status = {
        "cluster": {
            "inventory": {
                "available": {
                    "nodes": [
                        {"allocatable": {"cpu": huge}, "available": {"cpu": huge}},
                        {"allocatable": {"cpu": huge}, "available": {"cpu": huge}},
                    ]
                }
            }
        }
    }
    cap = from_provider_status(status)  # must not raise
    assert cap.available_fraction() is None


def test_a_valid_pair_alongside_a_corrupt_one_still_measures_the_valid_dimension() -> None:
    """The drop is per-DIMENSION, not per-node. A provider reporting good memory
    and garbage CPU is still rankable on memory — refusing the whole node would
    throw away a real measurement."""
    cap = from_provider_status(_one_node({"cpu": 100, "memory": 200}, {"cpu": True, "memory": 50}))
    assert cap.cpu is None
    assert cap.memory == pytest.approx(0.25)
    assert cap.available_fraction() == pytest.approx(0.25)
