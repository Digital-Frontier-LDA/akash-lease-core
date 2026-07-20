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

    def test_four_byte_json_is_still_json(self):
        # `{}` padded to 4 bytes must not be mistaken for an int32.
        assert parse_result_exit_code(b"{ } ", default=-1) == -1


class TestParseResultLenientVsStrict:
    """The two consumers disagreed; both behaviours must remain available."""

    BAD = [
        b"not json",
        b"[1,2]",
        json.dumps({"other": 1}).encode(),
        json.dumps({"exit_code": "0"}).encode(),
        json.dumps({"exit_code": None}).encode(),
    ]

    @pytest.mark.parametrize("payload", BAD)
    def test_lenient_returns_default(self, payload):
        assert parse_result_exit_code(payload) == -1

    @pytest.mark.parametrize("payload", BAD)
    def test_strict_raises(self, payload):
        with pytest.raises(MalformedResultFrame):
            parse_result_exit_code(payload, strict=True)

    def test_custom_default(self):
        assert parse_result_exit_code(b"not json", default=0) == 0

    def test_bool_is_not_an_exit_code(self):
        # bool subclasses int — must be rejected, not silently read as 1/0.
        assert parse_result_exit_code(json.dumps({"exit_code": True}).encode()) == -1


class TestDirectProviderUrl:
    def test_string_command(self):
        url = build_direct_provider_ws_url("h:8443", "12345", "1", "1", "web", "echo hi")
        assert url.startswith("wss://h:8443/lease/12345/1/1/shell?")
        assert "cmd0=%2Fbin%2Fsh" in url
        assert "cmd1=-c" in url
        assert "cmd2=echo%20hi" in url
        assert "service=web" in url

    def test_list_command_joined(self):
        url = build_direct_provider_ws_url("h:1", "1", "1", "1", "svc", ["ls", "-la", "/tmp"])
        assert "cmd2=ls%20-la%20%2Ftmp" in url

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
    def test_minimal(self):
        m = build_proxy_connect_message("wss://p/lease/1/1/1/shell")
        assert m == {"type": "websocket", "url": "wss://p/lease/1/1/1/shell"}

    def test_with_jwt(self):
        m = build_proxy_connect_message("wss://p/x", jwt="tok")
        assert m["auth"] == {"jwt": "tok"}

    def test_stdin_is_base64(self):
        import base64

        m = build_proxy_connect_message("wss://p/x", stdin_data="echo hi\n")
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
