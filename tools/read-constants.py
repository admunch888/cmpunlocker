#!/usr/bin/env python3
import io
import os
import re
import sys

try:
    import yaml
except ImportError:
    sys.exit("error: PyYAML is required to read constants.yaml "
             "(apt install python3-yaml)")

REQUIRED_PROFILE_KEYS = ("cfg1", "lmr", "fb_bytes", "label", "geometry_rewrite")


def hex_forms(text):
    m = re.fullmatch(r"0[xX]([0-9a-fA-F]+)", text.strip())
    if not m:
        return None
    digits = m.group(1).lower().lstrip("0") or "0"
    forms = {digits, digits.zfill(8), digits.zfill(4)}
    return {"0x" + f for f in forms}


def present(blob_lower, value):
    forms = hex_forms(value)
    if forms is None:
        return False
    return any(re.search(re.escape(f) + r"u?\b", blob_lower) for f in forms)


# build.sh keeps the always-applied patches in PATCH_ORDER and appends the
# opt-in groups only when --mclk-ndiv / --p2p ask for them. constants.yaml
# names the group in each unlock so a patch cannot quietly move between an
# always-on and an opt-in build.
PATCH_ARRAYS = (("base", "PATCH_ORDER"),
                ("mclk", "MCLK_PATCHES"),
                ("p2p", "P2P_PATCHES"))


def read_patch_arrays(build_sh):
    text = io.open(build_sh, encoding="utf-8").read()
    built = {}
    for group, array in PATCH_ARRAYS:
        m = re.search(array + r"=\(\n(.*?)\n\)", text, re.S)
        if not m:
            sys.exit("error: %s not found in %s" % (array, build_sh))
        for line in m.group(1).splitlines():
            name = line.strip()
            if not name:
                continue
            if name in built:
                sys.exit("error: %s appears in more than one patch array in %s"
                         % (name, build_sh))
            built[name] = group
    return built


def main():
    if len(sys.argv) != 5:
        sys.exit("usage: read-constants.py <constants.yaml> <patch-dir> "
                 "<build.sh> <profile>")
    cpath, patch_dir, build_sh, profile = sys.argv[1:5]

    with io.open(cpath, encoding="utf-8") as f:
        c = yaml.safe_load(f)

    profiles = c.get("profiles") or {}
    if profile not in profiles:
        sys.exit("error: unknown profile %r; constants.yaml defines %s"
                 % (profile, ", ".join(sorted(profiles))))
    p = profiles[profile]
    missing = [k for k in REQUIRED_PROFILE_KEYS if k not in p]
    if missing:
        sys.exit("error: profile %r is missing: %s"
                 % (profile, ", ".join(missing)))

    unlocks = c.get("unlocks") or {}
    if not unlocks:
        sys.exit("error: constants.yaml declares no unlocks")

    built = read_patch_arrays(build_sh)
    groups = dict(PATCH_ARRAYS)
    declared = {}

    problems = []
    for uname in sorted(unlocks):
        u = unlocks[uname] or {}
        pname = u.get("patch")
        if not pname:
            continue
        group = u.get("group", "base")
        if group not in groups:
            problems.append("unlock %s: unknown group %r (expected one of %s)"
                            % (uname, group, ", ".join(sorted(groups))))
            continue
        declared[pname] = group

    for name in sorted(set(built) - set(declared)):
        problems.append("patch %s is built but not declared in constants.yaml"
                        % name)
    for name in sorted(set(declared) - set(built)):
        problems.append("constants.yaml declares %s but no patch array in "
                        "build.sh builds it" % name)
    for name in sorted(set(declared) & set(built)):
        if declared[name] != built[name]:
            problems.append("constants.yaml puts %s in group %r but build.sh "
                            "builds it in %r" % (name, declared[name], built[name]))

    cache = {}
    for uname in sorted(unlocks):
        u = unlocks[uname] or {}
        pname = u.get("patch")
        if not pname:
            problems.append("unlock %s has no patch" % uname)
            continue
        ppath = os.path.join(patch_dir, pname)
        if not os.path.isfile(ppath):
            problems.append("unlock %s: missing %s" % (uname, ppath))
            continue
        if ppath not in cache:
            raw = io.open(ppath, encoding="utf-8", errors="replace").read()
            cache[ppath] = (raw, raw.lower())
        raw, blob = cache[ppath]

        #
        # Not every patch is a register poke. The P2P set gates on RM symbols
        # and the HBM set on a build.sh placeholder, so `requires` pins the
        # literal text those patches depend on: if upstream renames a symbol
        # the check fails here instead of the patch applying but doing nothing.
        #
        for token in (u.get("requires") or []):
            if token not in raw:
                problems.append("unlock %s: required text %r not found in %s"
                                % (uname, token, pname))
        for rname, r in sorted((u.get("registers") or {}).items()):
            addr = r.get("addr")
            if not addr or not present(blob, addr):
                problems.append("unlock %s: %s addr %s not found in %s"
                                % (uname, rname, addr, pname))
                continue
            val = r.get("value")
            if val and not present(blob, val):
                problems.append("unlock %s: %s value %s not found in %s"
                                % (uname, rname, val, pname))

    pt = c.get("passthrough") or {}
    if pt:
        regs = pt.get("gsp_boot_state") or {}
        if not regs:
            problems.append("passthrough block has no gsp_boot_state")
        for rname in sorted(regs):
            r = regs[rname] or {}
            for key in ("addr", "value"):
                if hex_forms(str(r.get(key, ""))) is None:
                    problems.append("passthrough %s: %s is not hex"
                                    % (rname, key))
        root = os.path.join(os.path.dirname(os.path.abspath(build_sh)), "..")
        helper = os.path.join(root, "tools", "gsp-restore.py")
        if not os.path.isfile(helper):
            problems.append("passthrough declared but tools/gsp-restore.py "
                            "missing")

        mod = pt.get("module")
        if not mod:
            problems.append("passthrough block has no module")
        else:
            src = os.path.join(root, "driver", "passthrough", mod + ".c")
            if not os.path.isfile(src):
                problems.append("passthrough module %s: %s missing"
                                % (mod, src))
            for rel in ("tools/passthrough.sh", "tools/passthrough-arm.sh"):
                fp = os.path.join(root, rel)
                if not os.path.isfile(fp):
                    problems.append("passthrough: %s missing" % rel)
                elif mod not in io.open(fp, encoding="utf-8").read():
                    problems.append("passthrough module %s not referenced in %s"
                                    % (mod, rel))

    if problems:
        sys.exit("error: common/constants.yaml does not match the patches:\n  "
                 + "\n  ".join(problems))

    out = [
        ("CFG1", p["cfg1"]),
        ("LMR", p["lmr"]),
        ("FB_BYTES", p["fb_bytes"]),
        ("UNLOCK_LABEL", p["label"]),
        ("SKIP_GEOMETRY_REWRITE", "0" if p["geometry_rewrite"] else "1"),
        ("PROFILE_STOCK_MIB", str(p.get("stock_mib", ""))),
        ("PROFILE_UNLOCKED_MIB", str(p.get("unlocked_mib", ""))),
        ("CONSTANTS_UNLOCK_COUNT", str(len(unlocks))),
    ]
    for k, v in out:
        print("%s=%s" % (k, "'" + str(v).replace("'", "'\\''") + "'"))


if __name__ == "__main__":
    main()
