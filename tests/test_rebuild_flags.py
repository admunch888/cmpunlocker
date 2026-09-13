"""
A kernel-update rebuild must replay the compile-time flags from build.conf.
If it does not, the card comes back at stock clocks with no error anywhere -
the modules exist, the service reports success, and only a benchmark would
ever notice.

These run rebuild.sh's verification logic against a stubbed build.sh, so no
kernel, GPU or root is needed.
"""
import os
import re
import subprocess

import pytest

import repo

REBUILD = repo.ROOT / "persist" / "rebuild.sh"
SRC = REBUILD.read_text()


def extract_verification():
    """Pull the marker check out of rebuild.sh so it can run standalone."""
    start = SRC.index('want_ndiv="${CMPUNLOCKER_MCLK_NDIV:-stock}"')
    end = SRC.index('log "verified:')
    return SRC[start:end]


def run_check(tmp_path, markers, conf, expect_fail):
    mod = tmp_path / "mods"
    mod.mkdir()
    for name, value in markers.items():
        (mod / name).write_text(value + "\n")
    script = tmp_path / "check.sh"
    body = extract_verification().replace('fail "', 'echo FAILED: "')
    script.write_text(
        "set -uo pipefail\n"
        'MOD_DIR="%s"\n' % mod +
        'CONF_FILE="build.conf"\n'
        "log() { echo \"$*\"; }\n"
        + "".join('%s="%s"\n' % (k, v) for k, v in conf.items())
        + body
    )
    proc = subprocess.run(["bash", str(script)], capture_output=True, text=True)
    out = proc.stdout + proc.stderr
    assert ("FAILED:" in out) == expect_fail, out
    return out


def test_matching_flags_pass(tmp_path):
    run_check(tmp_path,
              {"mclk_ndiv": "70", "p2p": "enabled", "driver_version": "615.71.09"},
              {"CMPUNLOCKER_MCLK_NDIV": "70", "CMPUNLOCKER_ENABLE_P2P": "1",
               "CMPUNLOCKER_DRIVER_VERSION": "615.71.09"},
              expect_fail=False)


def test_dropped_overclock_is_caught(tmp_path):
    # The exact silent regression: build.conf asks for 70, the rebuild produced
    # a stock-clocked module.
    out = run_check(tmp_path,
                    {"mclk_ndiv": "stock", "p2p": "enabled"},
                    {"CMPUNLOCKER_MCLK_NDIV": "70", "CMPUNLOCKER_ENABLE_P2P": "1"},
                    expect_fail=True)
    assert "mclk_ndiv" in out


def test_dropped_p2p_is_caught(tmp_path):
    out = run_check(tmp_path,
                    {"mclk_ndiv": "70", "p2p": ""},
                    {"CMPUNLOCKER_MCLK_NDIV": "70", "CMPUNLOCKER_ENABLE_P2P": "1"},
                    expect_fail=True)
    assert "p2p" in out


def test_wrong_driver_version_is_caught(tmp_path):
    out = run_check(tmp_path,
                    {"mclk_ndiv": "stock", "p2p": "", "driver_version": "610.57.04"},
                    {"CMPUNLOCKER_MCLK_NDIV": "", "CMPUNLOCKER_ENABLE_P2P": "",
                     "CMPUNLOCKER_DRIVER_VERSION": "615.71.09"},
                    expect_fail=True)
    assert "driver_version" in out


def test_stock_build_with_no_flags_passes(tmp_path):
    run_check(tmp_path,
              {"mclk_ndiv": "stock", "p2p": ""},
              {"CMPUNLOCKER_MCLK_NDIV": "", "CMPUNLOCKER_ENABLE_P2P": ""},
              expect_fail=False)


def test_missing_marker_file_is_caught(tmp_path):
    # An older payload that does not stamp the markers must not read as a pass.
    out = run_check(tmp_path, {},
                    {"CMPUNLOCKER_MCLK_NDIV": "70", "CMPUNLOCKER_ENABLE_P2P": "1"},
                    expect_fail=True)
    assert "mclk_ndiv" in out


def test_rebuild_sources_build_conf():
    # The whole chain depends on this one line.
    assert re.search(r'^\s*\.\s+"\$\{CONF_FILE\}"', SRC, re.M), \
        "rebuild.sh must source build.conf or the flags are lost"


def test_install_persist_records_the_flags():
    text = (repo.ROOT / "persist" / "install-persist.sh").read_text()
    for var in ("CMPUNLOCKER_MCLK_NDIV", "CMPUNLOCKER_ENABLE_P2P",
                "CMPUNLOCKER_DRIVER_VERSION", "CMPUNLOCKER_CARD_PROFILE"):
        assert var in text, var


def test_install_passes_the_flags_through():
    text = (repo.ROOT / "install.sh").read_text()
    block = text[text.index('step "Surviving kernel updates"'):]
    for var in ("CMPUNLOCKER_MCLK_NDIV", "CMPUNLOCKER_ENABLE_P2P"):
        assert var in block, var
