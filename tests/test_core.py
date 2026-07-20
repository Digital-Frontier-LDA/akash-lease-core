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
