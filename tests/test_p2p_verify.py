"""
The byte comparison in tools/p2p-verify.py is the whole point of the tool: if it
reports a mismatch that is not there, a working P2P link looks corrupt, and if it
misses one, a corrupt link looks fine. Both directions are pinned here.
"""
import ctypes
import importlib.util

import pytest

import repo

spec = importlib.util.spec_from_file_location(
    "p2p_verify", str(repo.ROOT / "tools" / "p2p-verify.py"))
p2p = importlib.util.module_from_spec(spec)
spec.loader.exec_module(p2p)

N = 4096


def buf(data):
    b = ctypes.create_string_buffer(len(data))
    ctypes.memmove(b, data, len(data))
    return b


def test_identical_buffers_report_no_mismatch():
    # The regression: a ctypes buffer is format '<c' and bytes is 'B', and
    # memoryviews with differing formats compare unequal whatever they hold.
    data = p2p.keyed_pattern(N, 0, 1)
    assert p2p.first_mismatch(buf(data), data, N) == -1


@pytest.mark.parametrize("pos", [0, 1, 37, N // 2, N - 1])
def test_single_flipped_byte_is_located(pos):
    data = bytearray(p2p.keyed_pattern(N, 0, 1))
    corrupt = bytearray(data)
    corrupt[pos] ^= 0xFF
    assert p2p.first_mismatch(buf(bytes(corrupt)), bytes(data), N) == pos


def test_untouched_destination_is_caught():
    # A copy that never lands leaves the 0xA5 sentinel the tool pre-fills.
    data = p2p.keyed_pattern(N, 0, 1)
    assert p2p.first_mismatch(buf(b"\xA5" * N), data, N) >= 0


def test_wrong_direction_pattern_is_caught():
    # Keying per direction means a stale buffer from the reverse copy fails.
    assert p2p.first_mismatch(buf(p2p.keyed_pattern(N, 1, 0)),
                              p2p.keyed_pattern(N, 0, 1), N) >= 0


def test_only_the_first_nbytes_are_compared():
    data = p2p.keyed_pattern(N, 0, 1)
    tail = bytearray(data)
    tail[-1] ^= 0xFF
    assert p2p.first_mismatch(buf(bytes(tail)), data, N - 1) == -1
