"""Contract tests for the sans-I/O lease-shell core.

These pin the wire format and — critically — the exec result semantics that the
two original implementations disagreed on. Each divergence is covered from BOTH
sides so the superset is provably compatible with each consumer.
"""

import json
import struct

import pytest

from akash_lease_core import (
    FAILURE,
    MAX_URL_CMD_BYTES,
    RESIZE,
    RESULT,
    STDERR,
    STDIN,
    STDOUT,
    TRACE_ENV,
    FrameTrace,
    MalformedResultFrame,
    build_direct_provider_ws_url,
    build_proxy_connect_message,
    command_needs_stdin_delivery,
    decode_frame,
    decode_proxy_payload,
    interpret_success,
    is_unverified_success,
    parse_result_exit_code,
)


class TestFrameConstants:
    def test_values(self):
        assert (STDOUT, STDERR, RESULT, FAILURE, STDIN, RESIZE) == (100, 101, 102, 103, 104, 105)


class TestDecodeFrame:
    def test_valid(self):
        assert decode_frame(bytes([STDOUT]) + b"hello") == (100, b"hello")

    def test_bytearray(self):
        assert decode_frame(bytearray([RESULT]) + bytearray(b"{}")) == (102, b"{}")

    def test_empty_is_none(self):
        assert decode_frame(b"") is None

    def test_non_bytes_is_none(self):
        assert decode_frame("nope") is None
        assert decode_frame(None) is None


class TestParseResultJSON:
    def test_zero(self):
        assert parse_result_exit_code(json.dumps({"exit_code": 0}).encode()) == 0

    def test_nonzero(self):
        assert parse_result_exit_code(json.dumps({"exit_code": 7}).encode()) == 7

    def test_negative(self):
        assert parse_result_exit_code(json.dumps({"exit_code": -1}).encode()) == -1


class TestParseResultRawInt32:
    """just-akash accepted a raw 4-byte LE int32; the control plane did not."""

    def test_zero(self):
        assert parse_result_exit_code(struct.pack("<i", 0)) == 0

    def test_nonzero(self):
        assert parse_result_exit_code(struct.pack("<i", 7)) == 7

    def test_negative(self):
        assert parse_result_exit_code(struct.pack("<i", -2)) == -2

    def test_four_byte_json_is_not_mistaken_for_int32(self):
        """A 4-byte payload that parses as JSON takes the JSON path.

        `b"{ } "` is exactly 4 bytes AND valid JSON for `{}` — it must be read
        as a keyless (therefore malformed) result frame, never as an int32.
        """
        with pytest.raises(MalformedResultFrame):
            parse_result_exit_code(b"{ } ")


class TestParseResultCanonical:
    """ONE behaviour, no flags: anything unparsable raises.

    v0.1.0 shipped strict/default flags so each consumer could keep its prior
    behaviour. A side-by-side measurement showed one of those behaviours was a
    BUG (a keyless frame returning 0 = success), so v0.2.0 prescribes a single
    canonical semantics and consumers converge to it.
    """

    MALFORMED = [
        b"not json",
        b"[1,2]",
        b"null",  # valid JSON -> None, AND exactly 4 bytes: must not hit the int32 path
        json.dumps({"other": 1}).encode(),  # missing exit_code
        json.dumps({"exit_code": None}).encode(),  # null
        json.dumps({"exit_code": "7"}).encode(),  # no int() coercion
        json.dumps({"exit_code": True}).encode(),  # bool is not an exit code
        json.dumps({"exit_code": "abc"}).encode(),
        b"\x00\x00\x00\x00\x00",  # 5 bytes: not the 4-byte form
    ]

    @pytest.mark.parametrize("payload", MALFORMED)
    def test_malformed_raises(self, payload):
        with pytest.raises(MalformedResultFrame):
            parse_result_exit_code(payload)

    def test_keyless_frame_is_never_a_silent_success(self):
        """The bug this release exists to fix: `{}` must not read as exit 0.

        The closed-lease failure mode emits {"exit_code": 0} with the key
        PRESENT, so a keyless frame is a distinct, unexplained condition.
        """
        with pytest.raises(MalformedResultFrame):
            parse_result_exit_code(b"{}")

    def test_int32_is_signed(self):
        """0xFFFFFFFF is -1 (an error), not 4294967295 (a garbage exit code)."""
        assert parse_result_exit_code(b"\xff\xff\xff\xff") == -1


class TestDirectProviderUrl:
    def test_string_command(self):
        url = build_direct_provider_ws_url("h:8443", "12345", "1", "1", "web", "echo hi")
        assert url.startswith("wss://h:8443/lease/12345/1/1/shell?")
        assert "cmd0=%2Fbin%2Fsh" in url
        assert "cmd1=-c" in url
        assert "cmd2=echo%20hi" in url
        assert "service=web" in url

    def test_list_command_is_argv_one_param_per_element(self):
        """A list is argv. It gets one cmdN each and NO shell wrapper.

        This replaces test_list_command_joined, which asserted
        `cmd2=ls%20-la%20%2Ftmp` -- i.e. it pinned the defect as the contract. That is
        why the bug survived two releases with a green suite.
        """
        url = build_direct_provider_ws_url("h:1", "1", "1", "1", "svc", ["ls", "-la", "/tmp"])
        assert "cmd0=ls" in url
        assert "cmd1=-la" in url
        assert "cmd2=%2Ftmp" in url
        assert "cmd2=ls%20-la%20%2Ftmp" not in url, "the old space-joined shape came back"

    def test_sh_dash_c_is_not_wrapped_a_second_time(self):
        """THE DEFECT, stated as its round trip.

        `["sh","-c","echo hi"]` used to become `/bin/sh -c "sh -c echo hi"`, where the
        inner `sh -c` took only `echo` as its command and `hi` became a positional
        parameter. Any caller shipping a script this way ran its first word and nothing
        else.
        """
        url = build_direct_provider_ws_url("h:1", "1", "1", "1", "svc", ["sh", "-c", "echo hi"])
        assert "cmd0=sh" in url
        assert "cmd1=-c" in url
        assert "cmd2=echo%20hi" in url
        assert "cmd0=%2Fbin%2Fsh" not in url, "a list must not be wrapped in /bin/sh -c"
        assert "cmd3=" not in url, "a nested wrap would have pushed the argv along by two"

    def test_argv_boundaries_survive_an_element_containing_spaces(self):
        """The space-join did not merely wrap twice -- it also re-split arguments.

        `["echo", "a b"]` must stay TWO parameters. Under the join it became the string
        `echo a b`, which the shell then re-split into three words, so an argument with
        a space silently became two arguments.
        """
        url = build_direct_provider_ws_url("h:1", "1", "1", "1", "svc", ["echo", "a b"])
        assert "cmd0=echo" in url
        assert "cmd1=a%20b" in url
        assert "cmd2=" not in url

    def test_string_command_keeps_its_shell_wrapper(self):
        """BACKWARD COMPATIBILITY CONTROL -- and it is the reason the string branch stayed.

        A string is a shell command LINE, so wrapping it is correct and unchanged. Only
        the list branch was wrong. Removing this wrapper would break every string caller
        AND drop shell builtins (`kill` is a builtin; a distroless image has no
        /bin/kill), which is precisely the risk that made this a decision rather than a
        default.
        """
        url = build_direct_provider_ws_url("h:1", "1", "1", "1", "svc", "kill -9 1")
        assert "cmd0=%2Fbin%2Fsh" in url
        assert "cmd1=-c" in url
        assert "cmd2=kill%20-9%201" in url

    def test_list_stdin_threshold_measures_the_encoded_payload(self):
        """A list's size is what its cmdN values cost, not a space-joined guess."""
        assert command_needs_stdin_delivery(["a" * MAX_URL_CMD_BYTES]) is False
        assert command_needs_stdin_delivery(["a" * MAX_URL_CMD_BYTES, "b"]) is True

    def test_oversized_list_still_falls_back_to_the_interactive_shell(self):
        """The STDIN path exists for URL LENGTH and is not what this change touches."""
        url = build_direct_provider_ws_url(
            "h:1", "1", "1", "1", "s", ["sh", "-c", "a" * (MAX_URL_CMD_BYTES + 10)]
        )
        assert "cmd0=%2Fbin%2Fsh" in url
        assert "cmd1=" not in url

    def test_oversized_drops_cmd2(self):
        url = build_direct_provider_ws_url(
            "h:1", "1", "1", "1", "s", "a" * (MAX_URL_CMD_BYTES + 10)
        )
        assert "cmd2=" not in url
        assert "cmd0=%2Fbin%2Fsh" in url

    def test_exact_boundary_keeps_cmd2(self):
        """At exactly the threshold the command still rides the URL."""
        url = build_direct_provider_ws_url("h:1", "1", "1", "1", "s", "a" * MAX_URL_CMD_BYTES)
        assert "cmd2=" in url


class TestStdinThreshold:
    def test_short(self):
        assert command_needs_stdin_delivery("echo hi") is False

    def test_over(self):
        assert command_needs_stdin_delivery("a" * (MAX_URL_CMD_BYTES + 1)) is True

    def test_exact_boundary_does_not_need_stdin(self):
        """Pins the off-by-one: `>=` instead of `>` flips this and nothing else."""
        assert command_needs_stdin_delivery("a" * MAX_URL_CMD_BYTES) is False


class TestProxyEnvelope:
    """The envelope shape is dictated by the Console proxy, not by us.

    v0.1.0 got this wrong (no providerAddress, no isBase64, and auth as
    {"jwt": ...}). These assertions pin the real shape.
    """

    def test_real_envelope_shape(self):
        m = build_proxy_connect_message("/lease/1/1/1/shell", "tok", "akash1prov")
        assert m == {
            "type": "websocket",
            "url": "/lease/1/1/1/shell",
            "providerAddress": "akash1prov",
            "auth": {"type": "jwt", "token": "tok"},
            "isBase64": True,
        }

    def test_auth_is_not_the_naive_shape(self):
        m = build_proxy_connect_message("/x", "tok", "akash1prov")
        assert m["auth"] != {"jwt": "tok"}, "auth must be {type, token}, not {jwt}"

    def test_stdin_is_base64(self):
        import base64

        m = build_proxy_connect_message("/x", "tok", "akash1prov", stdin_data="echo hi\n")
        assert base64.b64decode(m["data"]).decode() == "echo hi\n"


class TestProxyPayloadDecode:
    def test_valid_base64(self):
        import base64

        assert decode_proxy_payload(base64.b64encode(b"out").decode()) == b"out"

    def test_non_base64_discarded_on_shell_path(self):
        assert decode_proxy_payload("Received error from provider websocket") is None

    def test_non_base64_kept_on_logs_path(self):
        got = decode_proxy_payload("plain log line", text_fallback=True)
        assert got == b"plain log line"


class TestIsUnverifiedSuccess:
    def test_rc0_empty(self):
        assert is_unverified_success(0, "") is True
        assert is_unverified_success(0, "  \n") is True

    def test_rc0_with_output(self):
        assert is_unverified_success(0, "hi") is False

    def test_nonzero(self):
        assert is_unverified_success(1, "") is False


class TestInterpretSuccess:
    def test_nonzero_always_false(self):
        assert interpret_success(1, "anything") is False
        assert interpret_success(-1, "TOKEN", marker="TOKEN") is False

    def test_default_rc0_true(self):
        assert interpret_success(0, "") is True

    def test_marker_present(self):
        assert interpret_success(0, "pre TOKEN post", marker="TOKEN") is True

    def test_marker_absent_fails_on_rc0(self):
        """The closed-lease / dropped-stdout failure mode."""
        assert interpret_success(0, "", marker="TOKEN") is False

    def test_require_stdout(self):
        assert interpret_success(0, "   ", require_stdout=True) is False
        assert interpret_success(0, "data", require_stdout=True) is True

    def test_marker_takes_precedence(self):
        assert interpret_success(0, "TOKEN", marker="TOKEN", require_stdout=True) is True
        assert interpret_success(0, "noise", marker="TOKEN", require_stdout=True) is False


class TestSansIOInvariant:
    def test_core_imports_no_networking(self):
        """The package must never grow an I/O dependency."""
        import pathlib

        src = (
            pathlib.Path(__file__).parent.parent / "src" / "akash_lease_core" / "__init__.py"
        ).read_text()
        for banned in (
            "import socket",
            "import ssl",
            "import asyncio",
            "import websockets",
            "import requests",
            "import httpx",
        ):
            assert banned not in src, f"sans-I/O violated: {banned}"


class TestFrameTrace:
    """The instrument that separates drop / reorder / synthetic-zero / no-result."""

    @staticmethod
    def _f(code: int, payload: bytes = b"x") -> bytes:
        return bytes([code]) + payload

    def test_disabled_by_default_and_records_nothing(self):
        t = FrameTrace(enabled=False)
        t.record(self._f(STDOUT, b"hello"))
        assert not t.enabled
        assert t.frames == []
        assert t.classify() == "not_traced"

    def test_env_opt_in(self, monkeypatch):
        monkeypatch.setenv(TRACE_ENV, "1")
        assert FrameTrace().enabled
        for off in ("0", ""):
            monkeypatch.setenv(TRACE_ENV, off)
            assert not FrameTrace().enabled, f"{off!r} should not enable tracing"
        monkeypatch.delenv(TRACE_ENV, raising=False)
        assert not FrameTrace().enabled

    def test_records_code_and_length_but_NEVER_payload(self):
        """⛔ Payload bytes must never enter the trace — commands carry secrets."""
        t = FrameTrace(enabled=True)
        t.record(self._f(STDOUT, b"super-secret-token"))
        ((code, ln, rel),) = t.frames
        assert code == STDOUT and ln == len(b"super-secret-token")
        assert "super-secret" not in t.render(), "payload leaked into the rendered line"
        assert all(isinstance(x, (int, float)) for x in (code, ln, rel))

    def test_healthy_shape(self):
        t = FrameTrace(enabled=True)
        t.record(self._f(STDOUT, b"out"))
        t.record(self._f(RESULT, b'{"exit_code":0}'))
        assert t.shape() == "stdout,result"
        assert t.stdout_bytes() == 3
        assert t.classify() == "stdout_present"
        assert t.t_result is not None

    def test_a_lone_small_result_is_flagged(self):
        """The synthetic-zero SHAPE: one ~16-byte result frame, no stdout ever."""
        t = FrameTrace(enabled=True)
        t.record(self._f(RESULT, b'{"exit_code":0}'))
        assert t.classify() == "lone_small_result"
        assert t.stdout_bytes() == 0

    def test_a_reorder_is_distinguished_from_a_drop(self):
        """★ THE DISCRIMINATION THIS EXISTS FOR — same outcome, different mechanism."""
        drop = FrameTrace(enabled=True)
        drop.record(self._f(RESULT, b'{"exit_code":0}'))
        drop.record(self._f(STDERR, b"e"))
        assert drop.classify() == "no_stdout_frame"

        reorder = FrameTrace(enabled=True)
        reorder.record(self._f(RESULT, b'{"exit_code":0}'))
        reorder.record(self._f(STDOUT, b"late"))
        assert reorder.classify() == "reorder"
        assert drop.classify() != reorder.classify(), (
            "a drop and a reorder classify identically — the instrument cannot do its job"
        )

    def test_no_result_frame_is_its_own_state(self):
        t = FrameTrace(enabled=True)
        t.record(self._f(STDERR, b"boom"))
        assert t.classify() == "no_result_frame"
        assert t.t_result is None

    def test_no_frames_at_all(self):
        assert FrameTrace(enabled=True).classify() == "no_frames"

    def test_classify_is_not_a_single_valued_function(self):
        """★ NON-VACUITY. A classifier answering one label always would satisfy every
        assertion above that only checks 'not equal to the other one'."""
        seen = set()
        for frames in (
            [(STDOUT, b"o"), (RESULT, b"r")],
            [(RESULT, b"r")],
            [(RESULT, b"r"), (STDOUT, b"o")],
            [(STDERR, b"e")],
            [],
        ):
            t = FrameTrace(enabled=True)
            for c, p in frames:
                t.record(self._f(c, p))
            seen.add(t.classify())
        assert len(seen) >= 5, f"classify() collapses distinct sequences: {seen}"

    def test_render_is_deferred_and_self_describing(self):
        t = FrameTrace(enabled=True)
        t.record(self._f(STDOUT, b"ab"))
        t.record(self._f(RESULT, b'{"exit_code":0}'))
        line = t.render(recovered=2)
        for token in (
            "FRAME-TRACE",
            "shape=[stdout,result]",
            "stdout_bytes=2",
            "recovered=2",
            "classify=stdout_present",
            "t_result=",
        ):
            assert token in line, f"{token!r} missing from: {line}"


class TestRecordParts:
    def test_record_parts_matches_record(self):
        a, b = FrameTrace(enabled=True), FrameTrace(enabled=True)
        a.record(bytes([STDOUT]) + b"hello")
        b.record_parts(STDOUT, len(b"hello"))
        assert [(c, ln) for c, ln, _ in a.frames] == [(c, ln) for c, ln, _ in b.frames]
        assert a.classify() == b.classify() == "stdout_present"

    def test_record_parts_is_a_noop_when_disabled(self):
        t = FrameTrace(enabled=False)
        t.record_parts(STDOUT, 999)
        assert t.frames == [] and t.classify() == "not_traced"

    def test_record_parts_sets_t_result(self):
        t = FrameTrace(enabled=True)
        t.record_parts(RESULT, 15)
        assert t.t_result is not None and t.classify() == "lone_small_result"


class TestCloseDiscriminator:
    """★ ARCHITECT's two candidates for `exit=-1` BOTH produce zero frames.

    An empty-stderr `-1` can arise where the peer closed the connection, or where the
    loop ended without a result frame. `classify()` reads what ARRIVED; only the close
    reason reads why it STOPPED — and with no frames those are the same reading.
    """

    def test_no_frames_alone_cannot_separate_the_two_candidates(self):
        """The gap this exists to fill, asserted rather than assumed."""
        a, b = FrameTrace(enabled=True), FrameTrace(enabled=True)
        a.close("peer_closed")
        b.close("loop_ended_no_result")
        assert a.classify() == b.classify() == "no_frames", (
            "if these ever classify differently the close reason is redundant — delete it"
        )
        assert a.close_reason != b.close_reason, "the close reason does not separate them"

    def test_close_records_a_time_on_the_same_clock_as_frames(self):
        t = FrameTrace(enabled=True)
        t.record_parts(STDOUT, 4)
        t.close("peer_closed")
        assert t.close_at is not None and t.frames
        assert t.close_at >= t.frames[-1][2], (
            "close_at precedes the last frame — the two are not on the same t0"
        )

    def test_close_is_a_noop_when_disabled(self):
        t = FrameTrace(enabled=False)
        t.close("peer_closed")
        assert t.close_reason is None and t.close_at is None

    def test_render_surfaces_the_close(self):
        t = FrameTrace(enabled=True)
        t.close("peer_closed")
        line = t.render()
        assert "close=peer_closed@" in line, f"the close reason is not in the trace line: {line}"
        assert "shape=[]" in line and "classify=no_frames" in line

    def test_render_says_n_a_rather_than_inventing_a_close(self):
        """⚠ An un-closed trace must not read as a close at t=0."""
        t = FrameTrace(enabled=True)
        t.record_parts(RESULT, 15)
        assert "close=n/a@n/a" in t.render()


class TestClassifyEnumerationIsComplete:
    """Every label `classify()` can return must be produced by a real sequence.

    ★ WHY `len(seen) >= 5` IS NOT THIS TEST. A lower bound says the classifier is not
    single-valued. It cannot say whether a label exists that NO sequence in the corpus
    reaches — and an unreached branch is the dangerous one: it will be read for the first
    time in production, on the run that matters, with no fixture ever having exercised it.
    `reorder` was exactly that for a while on the consumer side — a branch the real client
    could not reach, so the classifier had an answer nobody could ever see.

    The declared set is read out of the FUNCTION, not listed here, so a label added
    without a fixture fails on the day it is added rather than the day it is needed.
    """

    @staticmethod
    def _declared() -> set:
        import ast
        import inspect
        import textwrap

        tree = ast.parse(textwrap.dedent(inspect.getsource(FrameTrace.classify)))
        return {
            n.value.value
            for n in ast.walk(tree)
            if isinstance(n, ast.Return)
            and isinstance(n.value, ast.Constant)
            and isinstance(n.value.value, str)
        }

    @staticmethod
    def _corpus() -> dict:
        def trace(frames, enabled=True):
            t = FrameTrace(enabled=enabled)
            for code, length in frames:
                t.record_parts(code, length)
            return t

        return {
            "tracing off": trace([], enabled=False),
            "nothing arrived": trace([]),
            "stdout then result": trace([(STDOUT, 12), (RESULT, 15)]),
            "result then stdout (drain recovery)": trace([(RESULT, 15), (STDOUT, 12)]),
            "a lone small result": trace([(RESULT, 15)]),
            "stderr then result": trace([(STDERR, 20), (RESULT, 15)]),
            "stderr, session ended": trace([(STDERR, 20)]),
        }

    def test_the_declared_set_was_actually_read(self):
        """Control. An empty declared set makes the comparison below vacuous."""
        declared = self._declared()
        assert len(declared) >= 5, (
            f"only {declared} read out of classify() — the AST reader is stale"
        )

    def test_every_label_the_classifier_can_return_has_a_sequence_that_produces_it(self):
        declared = self._declared()
        produced = {t.classify() for t in self._corpus().values()}

        unreached = sorted(declared - produced)
        assert not unreached, (
            f"classify() can return {unreached}, and no sequence in the corpus produces "
            "them. An unexercised branch is read for the first time in production, on the "
            "run that matters. Add a sequence that reaches it, or delete the branch."
        )
        invented = sorted(produced - declared)
        assert not invented, (
            f"the corpus produced {invented}, which the AST reader did not find among "
            "classify()'s returns — the reader is missing a return path, so 'unreached' "
            "above is measured against an incomplete declaration."
        )

    def test_distinct_sequences_stay_distinct(self):
        """The corpus is chosen so no two entries may share a label.

        ⚠ Each pair here is a decision someone makes differently: a drop is the provider's
        problem, a reorder is ours, a lone small result is a dead lease. If two of them
        ever answer the same, the classifier has stopped being a classifier — and the
        assertion above would still pass, because every label would still be produced.
        """
        labels = {name: t.classify() for name, t in self._corpus().items()}
        collisions = [
            (a, b)
            for i, a in enumerate(labels)
            for b in list(labels)[i + 1 :]
            if labels[a] == labels[b]
        ]
        assert not collisions, f"these sequences classify identically: {collisions} — {labels}"


def test_the_two_declarations_of_the_version_agree():
    """★ The package states its version TWICE, and a bump touches one of them.

    ⚠ FOUND BY WALKING INTO IT. Bumping `pyproject.toml` for the 0.3.0 release left
    `__version__` reading "0.2.0" — the wheel would have installed as 0.3.0 while the
    module it contains introduced itself as 0.2.0.

    That is not cosmetic here. The consumer pins by URL and `#sha256=`, so the wheel is
    unambiguous, but ANY runtime check — a log line, a compatibility guard, a support
    question answered from `akash_lease_core.__version__` — reads the copy that did not
    move. A version that lies is worse than one that is absent: absence prompts a
    question, a wrong answer ends the enquiry.

    Same shape as the two requirements pins in the consumer repo: one fact, two
    declarations, each individually valid, and nothing asserting they agree.
    """
    import re
    from pathlib import Path

    import akash_lease_core

    pyproject = (Path(__file__).resolve().parents[1] / "pyproject.toml").read_text()
    match = re.search(r'^version\s*=\s*"([^"]+)"', pyproject, re.M)
    assert match, "pyproject.toml no longer declares a version — this test's premise moved"

    assert match.group(1) == akash_lease_core.__version__, (
        f"pyproject.toml says {match.group(1)} and __version__ says "
        f"{akash_lease_core.__version__}. A release bumps one of these; nothing but this "
        "assertion notices when it does not bump the other."
    )
