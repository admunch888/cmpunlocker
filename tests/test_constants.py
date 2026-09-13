import io
import subprocess
import sys

import pytest
import yaml

import repo

CONSTANTS = repo.ROOT / "common" / "constants.yaml"
READER = repo.ROOT / "tools" / "read-constants.py"
PATCHES = repo.ROOT / "driver" / "patches"
BUILD_SH = repo.ROOT / "driver" / "build.sh"

with io.open(CONSTANTS, encoding="utf-8") as f:
    DOC = yaml.safe_load(f)

PROFILES = sorted(DOC["profiles"])
EXPECTED_VARS = {"CFG1", "LMR", "FB_BYTES", "UNLOCK_LABEL", "SKIP_GEOMETRY_REWRITE",
                 "PROFILE_STOCK_MIB", "PROFILE_UNLOCKED_MIB", "CONSTANTS_UNLOCK_COUNT"}


def read_constants(profile):
    return subprocess.run(
        [sys.executable, str(READER), str(CONSTANTS), str(PATCHES),
         str(BUILD_SH), profile],
        capture_output=True, text=True)


@pytest.mark.parametrize("profile", PROFILES)
def test_constants_match_patches(profile):
    # Checks every declared register address and `requires` token against the
    # patch text, and that each patch sits in the build.sh array its group names.
    r = read_constants(profile)
    assert r.returncode == 0, r.stdout + r.stderr
    emitted = {l.split("=", 1)[0] for l in r.stdout.splitlines() if "=" in l}
    assert EXPECTED_VARS <= emitted, EXPECTED_VARS - emitted


def test_unknown_profile_is_rejected():
    assert read_constants("no-such-profile").returncode != 0


def test_every_unlock_declares_a_known_group():
    groups = {"base", "mclk", "p2p"}
    for name, unlock in sorted((DOC.get("unlocks") or {}).items()):
        assert (unlock or {}).get("group", "base") in groups, name


def test_optional_patches_are_declared_optional():
    # The opt-in patches must not be declared as base, or build.sh would be
    # expected to apply them unconditionally.
    optional = repo.optional_patches()
    unlocks = DOC.get("unlocks") or {}
    for group, names in sorted(optional.items()):
        for patch in names:
            declared = [u for u in unlocks.values()
                        if (u or {}).get("patch") == patch]
            assert declared, "%s is not declared in constants.yaml" % patch
            assert declared[0].get("group") == group, patch
