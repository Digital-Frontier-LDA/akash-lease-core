"""Read-function sentinel contract — candidate D, issue #17.

Pins the **public read-function discipline** that applies to I/O ADAPTERS
built on top of this package. The package itself is sans-I/O by charter (see
README "Invariants"), so it cannot split the (a)/(b)/(c) outcomes for an
incoming message — only the adapter that received the message can. This
file codifies the rule and pins the already-compliant public surface.

The defect the rule catches: a read function that returns the same value for
"I asked and got nothing" and "I could not ask". Measured consumer-side
instance: a sweeper that closed a 200 GiB volume three times because its
liveness read returned ``None`` for both transport failure and a 404 from
the upstream service. The destructive-action gate then fell through to an
age rule and treated "asked, got nothing" as a destructive-action safe
input. The discipline prevents the same shape at the adapter boundary.

Rule, in one sentence: every public ADAPTER read function MUST distinguish
three outcomes on a single axis —

    (a) instrument failure     — could not ask         → the value ``None``
    (b) asked, no answer       — successful empty read → a sentinel
                                                              (:data:`EMPTY_ATTRIBUTES`)
    (c) asked, full answer     — a real, non-empty value

Only (a) may block a destructive action downstream; only the ADAPTER can
know the difference, because only the adapter made the call. The pure
package cannot.

Pure transformations over already-in-hand inputs (e.g.
``is_unverified_success``, ``interpret_success``, ``rank_wallets``,
``Auction.evaluate``) are NOT read functions and have no instrument-failure
mode — they are out of scope.
"""

from __future__ import annotations

from akash_lease_core import (
    EMPTY_ATTRIBUTES,
    FrameTrace,
    decode_frame,
    decode_proxy_payload,
)


# ---------------------------------------------------------------------------
# 1. The sentinel exists and is a real, distinct value for ADAPTER use
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
# 2. decode_frame — DELIBERATELY EXCLUDED from candidate (D)
# ---------------------------------------------------------------------------
class TestDecodeFrameDeliberatelyExcluded:
    """``decode_frame`` is a pure parser. It asks nothing.

    A regression that returns ``EMPTY_ATTRIBUTES`` for ``b""`` would crash the
    only live consumer (``provider_shell_client`` in Blazing-Back, two call
    sites guarding on ``if frame is None: continue`` then unpacking to
    ``(code, payload)``). The contract test here pins the
    ``None``-for-everything-malformed contract so a future refactor cannot
    silently re-introduce the candidate (D) routing at this layer.
    """

    def test_non_bytes_is_none(self):
        # (a) instrument failure is the right semantics for a non-bytes input
        # to a parser: it is malformed, not "the instrument was asked and got
        # nothing".
        assert decode_frame("not bytes") is None
        assert decode_frame(None) is None
        assert decode_frame(42) is None

    def test_empty_bytes_is_none(self):
        # An empty message to a frame parser is malformed input. Returning
        # ``EMPTY_ATTRIBUTES`` here would break the only two live call sites
        # that guard on ``is None`` and then unpack; this test is the canary.
        assert decode_frame(b"") is None
        assert decode_frame(bytearray(b"")) is None

    def test_a_real_frame_with_an_empty_payload_is_still_a_real_frame(self):
        # (c) — a single code byte with no payload is a real frame, NOT an
        # empty read. ``(0, b"")`` and ``None`` and ``EMPTY_ATTRIBUTES`` must
        # all stay distinct.
        result = decode_frame(bytes([100]))
        assert result == (100, b"")
        assert result is not None
        assert result is not EMPTY_ATTRIBUTES


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


# ---------------------------------------------------------------------------
# 6. Adapter-side documentation of the rule (a meta-test pinning the rule
#    itself, not any single function).
# ---------------------------------------------------------------------------
class TestRuleAppliesAtAdapterBoundary:
    """The (a)/(b)/(c) split belongs at the I/O ADAPTER. This rule is encoded
    in the package's module docstring and re-pinned here so a future edit
    cannot soften it without a matching test failure.
    """

    def test_module_docstring_states_the_three_outcomes(self):
        import pathlib

        src = (
            pathlib.Path(__file__).parent.parent / "src" / "akash_lease_core" / "__init__.py"
        ).read_text()
        assert "EMPTY_ATTRIBUTES" in src
        assert "instrument failure" in src
        assert "asked, no answer" in src
        assert "asked, full answer" in src

    def test_decode_frame_docstring_records_the_deliberate_exclusion(self):
        import pathlib

        src = (
            pathlib.Path(__file__).parent.parent / "src" / "akash_lease_core" / "__init__.py"
        ).read_text()
        # The exclusion is a contract, not a TODO. If the wording is removed
        # in a future refactor, this test fails and the refactor must
        # explain why.
        assert "DELIBERATELY EXCLUDED" in src
        assert "provider_shell_client" in src
        assert "pure parser" in src
