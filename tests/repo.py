import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parent.parent
SKIP_DIRS = {".git", ".build", "__pycache__"}


def _files(suffix):
    return sorted(p for p in ROOT.rglob("*" + suffix)
                  if not SKIP_DIRS & set(p.relative_to(ROOT).parts))


def sh_files():
    return _files(".sh")


def py_files():
    return _files(".py")


def rel(path):
    return str(path.relative_to(ROOT))


def versions():
    lines = (ROOT / "driver" / "VERSION").read_text().splitlines()
    return [v for v in lines if re.fullmatch(r"\d+\.\d+\.\d+", v)]


def _patch_array(name):
    text = (ROOT / "driver" / "build.sh").read_text()
    m = re.search(name + r"=\(\n(.*?)\n\)", text, re.S)
    assert m, "%s not found in driver/build.sh" % name
    return m.group(1).split()


def patch_order():
    return _patch_array("PATCH_ORDER")


def optional_patches():
    """Patch groups build.sh appends to PATCH_ORDER only when the matching
    opt-in flag is set (--mclk-ndiv / --p2p)."""
    return {"mclk": _patch_array("MCLK_PATCHES"),
            "p2p": _patch_array("P2P_PATCHES")}


def all_patches():
    names = list(patch_order())
    for group in optional_patches().values():
        names += group
    return names
