import os
import pathlib
import shutil
import subprocess
import tempfile
import urllib.request

import pytest

import repo

URL = "https://github.com/NVIDIA/open-gpu-kernel-modules/archive/refs/tags/%s.tar.gz"
CACHE = pathlib.Path(os.environ.get("CMPUNLOCKER_BUILD_DIR", repo.ROOT / "driver" / ".build"))
PATCHES = repo.ROOT / "driver" / "patches"
ORDER = repo.patch_order()
OPTIONAL = repo.optional_patches()
ALL = repo.all_patches()
VERSIONS = repo.versions()
assert VERSIONS, "driver/VERSION lists no versions"


def tarball(version):
    path = CACHE / ("open-gpu-kernel-modules-%s.tar.gz" % version)
    if not path.is_file():
        CACHE.mkdir(parents=True, exist_ok=True)
        partial = path.with_name(path.name + ".partial")
        with urllib.request.urlopen(URL % version, timeout=60) as resp, open(partial, "wb") as out:
            shutil.copyfileobj(resp, out)
        partial.replace(path)
    return path


def test_patch_order_covers_patch_dir():
    # PATCH_ORDER holds the always-applied set; MCLK_PATCHES and P2P_PATCHES are
    # appended by build.sh only for --mclk-ndiv / --p2p builds. Every file in
    # driver/patches must be reachable from exactly one of those arrays.
    assert sorted(ALL) == sorted(p.name for p in PATCHES.glob("*.patch"))
    assert len(ALL) == len(set(ALL)), "a patch is listed in more than one array"


def _apply(names, version):
    assert shutil.which("patch"), "GNU patch is not installed"
    with tempfile.TemporaryDirectory() as tmp:
        subprocess.run(["tar", "-xzf", str(tarball(version)), "-C", tmp], check=True)
        src = pathlib.Path(tmp, "open-gpu-kernel-modules-" + version)
        assert src.is_dir(), os.listdir(tmp)
        for name in names:
            r = subprocess.run(["patch", "-p1", "-i", str(PATCHES / name)], cwd=src,
                               stdin=subprocess.DEVNULL, capture_output=True, text=True)
            assert r.returncode == 0, "%s on %s:\n%s%s" % (name, version, r.stdout, r.stderr)


@pytest.mark.parametrize("version", VERSIONS)
def test_patches_apply(version):
    _apply(ORDER, version)


@pytest.mark.parametrize("version", VERSIONS)
@pytest.mark.parametrize("group", sorted(OPTIONAL))
def test_optional_patches_apply(group, version):
    # The opt-in groups ride on top of PATCH_ORDER, exactly as build.sh appends
    # them, so they have to apply to every driver listed in driver/VERSION.
    _apply(ORDER + OPTIONAL[group], version)
