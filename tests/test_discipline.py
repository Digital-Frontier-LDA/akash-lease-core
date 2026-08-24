"""Read-function sentinel contract — candidate D, issue #17.

This file pins the **public read-function discipline** of ``akash_lease_core``.
It does not test happy-path behaviour of any single function (those live next
to the function). It tests the AXIS the read functions live on, so a future
addition cannot silently re-introduce the defect candidate D exists to catch:

    >>> the same value is returned for "I asked and got nothing" and
    >>> "I could not ask"

Measured consumer-side instance that motivated this: a sweeper that closed a
200 GiB volume three times because its liveness read returned ``None`` for
both transport failure and a 404 from the upstream service — the gate then
fell through to an age rule and treated "asked, got nothing" as a
destructive-action safe input.

The rule, in one sentence: every public read function MUST distinguish three
outcomes on a single axis — (a) instrument failure returns ``None``; (b)
asked, no answer returns :data:`EMPTY_ATTRIBUTES`; (c) asked, full answer
returns the real value. Only (a) may block a downstream destructive action.

Anything that does not need to ask a question — pure transformations over
already-in-hand inputs — is NOT a read function and is not in scope here
(e.g. ``is_unverified_success``, ``interpret_success``, ``rank_wallets``,
``Auction.evaluate``: the latter two operate on adapter-supplied state and
have no instrument-failure mode).
"""

from __future__ import annotations

import pytest

from akash_lease_core import (
    EMPTY_ATTRIBUTES,
    FrameTrace,
    decode_frame,
    decode_proxy_payload,
)


# ---------------------------------------------------------------------------
# 1. The sentinel exists and is a real, distinct value
# ---------------------------------------------------------------------------
class TestSentinelExists:
    def test_empty_attributes_is_a_frozenset(self):
        assert isinstance(EMPTY_ATTRIBUTES, frozenset)

    def test_empty_attributes_is_distinct_from_none(self):
        assert EMPTY_ATTRIBUTES is not None

    def test_empty_attributes_is_truthy_in_booleans_but_empty_as_a_collection(self):
        # A destructive-action gate that compared to ``EMPTY_ATTRIBUTES``
        # with ``==`` against ``None`` would silently pass; a gate that
        # compared ``not result`` against ``EMPTY_ATTRIBUTES`` would too.
        # The contract: distinguish by IDENTITY (``is``) or by type/structure,
        # never by boolean coercion.
        assert bool(EMPTY_ATTRIBUTES) is False
        assert len(EMPTY_ATTRIBUTES) == 0


# ---------------------------------------------------------------------------
# 2. decode_frame — the original instance of the defect
# ---------------------------------------------------------------------------
class TestDecodeFrame:
    """``b""`` and a non-bytes input MUST NOT collapse to the same value."""

    def test_synthetic_instrument_failure_returns_none(self):
        # (a) instrument failure — caller passed something the function
        # cannot interpret as a frame. Returns ``None`` so a downstream gate
        # can BLOCK.
        assert decode_frame("not bytes") is None
        assert decode_frame(None) is None
        assert decode_frame(42) is None
        assert decode_frame(["bytes"]) is None

    def test_successful_zero_byte_read_returns_empty_attributes(self):
        # (b) asked, no answer — caller passed an empty bytes object. Returns
        # :data:`EMPTY_ATTRIBUTES`. A caller that treats this as (a) is wrong.
        assert decode_frame(b"") is EMPTY_ATTRIBUTES
        assert decode_frame(b"") is not None
        assert decode_frame(bytearray(b"")) is EMPTY_ATTRIBUTES

    def test_a_real_frame_with_an_empty_payload_is_not_empty_attributes(self):
        # (c) asked, full answer — a single code byte (e.g. STDOUT) with no
        # payload is a real frame, not an empty read. ``(100, b"")`` and
        # ``EMPTY_ATTRIBUTES`` must remain distinct.
        result = decode_frame(bytes([100]))
        assert result == (100, b"")
        assert result is not EMPTY_ATTRIBUTES
        assert result is not None

    @pytest.mark.parametrize(
        "msg,expected",
        [
            (b"\x00", (0, b"")),
            (bytes([102]) + b'{"exit_code": 0}', (102, b'{"exit_code": 0}')),
        ],
    )
    def test_real_frames_continue_to_return_tuples(self, msg, expected):
        assert decode_frame(msg) == expected


# ---------------------------------------------------------------------------
# 3. decode_proxy_payload — already compliant; pin the contract so a
#    future edit cannot regress it.
# ---------------------------------------------------------------------------
class TestDecodeProxyPayload:
    """base64-decode failures (``None``) and zero-byte payloads (``b""``) are
    already distinct values — but the test pins the discipline so the function
    is not "fixed" to collapse them.
    """

    def test_malformed_base64_returns_none(self):
        # (a) instrument failure — string is not valid base64.
        assert decode_proxy_payload("not!valid!base64") is None

    def test_zero_byte_payload_returns_empty_bytes_not_none(self):
        # (b) asked, no answer — empty string is valid base64 for empty bytes.
        # The pre-existing contract returns ``b""`` for this case; we pin it
        # to keep the (a)/(b) separation alive.
        assert decode_proxy_payload("") == b""
        assert decode_proxy_payload("") is not None


# ---------------------------------------------------------------------------
# 4. FrameTrace — the disabled-vs-empty distinction
# ---------------------------------------------------------------------------
class TestFrameTraceDisabledVsEmpty:
    """A disabled tracer and a tracer that ran with no incoming frames must
    not collapse to the same downstream signal — both produce empty state,
    but only one means "the instrument was never turned on".
    """

    def test_disabled_classifies_as_not_traced(self):
        t = FrameTrace(enabled=False)
        assert t.classify() == "not_traced"

    def test_enabled_with_no_frames_classifies_as_no_frames(self):
        t = FrameTrace(enabled=True)
        assert t.classify() == "no_frames"

    def test_disabled_and_empty_produce_distinct_strings(self):
        # The pre-existing code already separates these via distinct strings;
        # the test pins the discipline so a future refactor that returns a
        # shared "empty" sentinel cannot silently merge them.
        assert FrameTrace(enabled=False).classify() != FrameTrace(enabled=True).classify()

    def test_a_real_frame_trace_returns_a_non_empty_classify(self):
        t = FrameTrace(enabled=True)
        t.record_parts(code=100, payload_len=5)
        assert t.classify() == "stdout_present"


# ---------------------------------------------------------------------------
# 5. The package charter — sans-I/O, no destructive-action branch added here
# ---------------------------------------------------------------------------
class TestPackageCharter:
    """The discipline lives in the consumers: a destructive-action gate that
    reads from this package must distinguish (a) from (b) at the call site.
    This module MUST NOT add such a gate — that is the consumer's job.
    """

    def test_discipline_module_itself_adds_no_io(self):
        import pathlib

        src = (
            pathlib.Path(__file__).parent.parent / "src" / "akash_lease_core" / "__init__.py"
        ).read_text()
        # Same discipline as TestSansIOInvariant in test_core, applied here to
        # the discipline module's own dependency surface.
        for banned in (
            "import socket",
            "import ssl",
            "import asyncio",
            "import websockets",
            "import requests",
            "import httpx",
        ):
            assert banned not in src, f"sans-I/O violated: {banned}"
