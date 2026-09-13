#!/usr/bin/env python3
"""
Verify real BAR1 peer-to-peer between CMP 170HX cards.

cudaDeviceCanAccessPeer() and `nvidia-smi topo -p2p` report what the driver
*claims*, and the --p2p patches work by changing exactly that claim. So neither
is evidence. This copies a keyed pattern device-to-device and reads the
destination back over PCIe to confirm the bytes actually arrived, in both
directions, for every pair.

Talks to libcuda directly: no CUDA toolkit, no PyTorch, nothing beyond the
installed driver.

  sudo ./tools/p2p-verify.py [--size-mb 256] [--iters 5]

Exit status is 0 only if every ordered pair moved correct bytes.
"""
import argparse
import ctypes
import os
import random
import re
import subprocess
import sys
import time

CMP_DEVICE_IDS = {"20c2", "2082"}

# Gen3 x16 is ~16 GB/s and Gen2 x16 ~8 GB/s, so anything past this is not a
# transfer rate - it means the copy was still in flight when the clock stopped.
PCIE_CEILING_GBPS = 30.0

# CUresult values we special-case
CUDA_SUCCESS = 0
CUDA_ERROR_PEER_ACCESS_ALREADY_ENABLED = 704


def load_libcuda():
    for name in ("libcuda.so.1", "libcuda.so"):
        try:
            return ctypes.CDLL(name)
        except OSError:
            continue
    sys.exit("error: libcuda.so.1 not found — is the NVIDIA driver installed and loaded?")


CUresult = ctypes.c_int
CUdevice = ctypes.c_int
CUcontext = ctypes.c_void_p
CUdeviceptr = ctypes.c_ulonglong


class Cuda:
    """
    Thin libcuda binding.

    Two things matter here. Every entry point gets explicit argtypes, because
    ctypes defaults to C int and would truncate the 64-bit pointers and
    size_t. And each symbol is resolved as <name>_v2 first: libcuda keeps the
    pre-CUDA-3.2 ABI under the plain name (32-bit sizes) and the current one
    under _v2, which is what the headers #define the plain name to. Calling
    the plain symbol gets the legacy signature and fails with
    CUDA_ERROR_INVALID_CONTEXT.
    """

    # Only these four have a v2 ABI in libcuda (the pre-CUDA-3.2 originals took
    # 32-bit sizes). Probing "<name>_v2" for everything risks binding an
    # unrelated versioned symbol, so the list is explicit.
    V2_SYMBOLS = {"cuMemAlloc", "cuMemFree", "cuMemcpyHtoD", "cuMemcpyDtoH"}

    SIGNATURES = {
        "cuGetErrorString":         [CUresult, ctypes.POINTER(ctypes.c_char_p)],
        "cuInit":                   [ctypes.c_uint],
        "cuDeviceGetCount":         [ctypes.POINTER(ctypes.c_int)],
        "cuDeviceGet":              [ctypes.POINTER(CUdevice), ctypes.c_int],
        "cuDeviceGetName":          [ctypes.c_char_p, ctypes.c_int, CUdevice],
        "cuDeviceGetPCIBusId":      [ctypes.c_char_p, ctypes.c_int, CUdevice],
        "cuDevicePrimaryCtxRetain": [ctypes.POINTER(CUcontext), CUdevice],
        "cuCtxSetCurrent":          [CUcontext],
        "cuCtxSynchronize":         [],
        "cuDeviceCanAccessPeer":    [ctypes.POINTER(ctypes.c_int), CUdevice, CUdevice],
        "cuCtxEnablePeerAccess":    [CUcontext, ctypes.c_uint],
        "cuMemAlloc":               [ctypes.POINTER(CUdeviceptr), ctypes.c_size_t],
        "cuMemFree":                [CUdeviceptr],
        "cuMemcpyHtoD":             [CUdeviceptr, ctypes.c_void_p, ctypes.c_size_t],
        "cuMemcpyDtoH":             [ctypes.c_void_p, CUdeviceptr, ctypes.c_size_t],
        "cuMemcpyPeer":             [CUdeviceptr, CUcontext, CUdeviceptr, CUcontext,
                                     ctypes.c_size_t],
    }

    def __init__(self):
        self._fns = {}
        self.lib = load_libcuda()
        self.bound = {}
        for name, argtypes in self.SIGNATURES.items():
            candidates = ([name + "_v2", name] if name in self.V2_SYMBOLS else [name])
            fn = None
            for sym in candidates:
                try:
                    fn = getattr(self.lib, sym)
                    self.bound[name] = sym
                    break
                except AttributeError:
                    continue
            if fn is None:
                sys.exit("error: %s not found in libcuda — driver too old?" % name)
            fn.argtypes = argtypes
            fn.restype = CUresult
            self._fns[name] = fn

    def check(self, rc, what):
        if rc == CUDA_SUCCESS:
            return
        msg = ctypes.c_char_p()
        self._fns["cuGetErrorString"](rc, ctypes.byref(msg))
        detail = msg.value.decode() if msg.value else "unknown"
        lines = ["error: %s failed: %s (%d)" % (what, detail, rc)]
        if rc in (709, 999, 4):
            # 709 CONTEXT_IS_DESTROYED / 999 UNKNOWN / 4 DEINITIALIZED all show up
            # when the GPU itself faulted mid-operation rather than when the call
            # was malformed.
            lines += [
                "",
                "That class of error usually means the device faulted during the",
                "operation rather than that the call was wrong. Check for an Xid:",
                "    sudo dmesg | grep -iE 'xid|nvrm' | tail -30",
                "    nvidia-smi -q | grep -iE 'pending|retired|remap|ecc'",
                "and retry smaller to see if it is size-dependent:",
                "    sudo ./tools/p2p-verify.py --size-mb 16 --iters 1",
            ]
        sys.exit("\n".join(lines))

    def __getattr__(self, name):
        fns = self.__dict__.get("_fns", {})
        if name in fns:
            return fns[name]
        raise AttributeError(name)


def pci_bus_id(cu, dev):
    buf = ctypes.create_string_buffer(32)
    cu.check(cu.cuDeviceGetPCIBusId(buf, 32, dev), "cuDeviceGetPCIBusId")
    return buf.value.decode().lower()


def device_name(cu, dev):
    buf = ctypes.create_string_buffer(128)
    cu.check(cu.cuDeviceGetName(buf, 128, dev), "cuDeviceGetName")
    return buf.value.decode()


def lspci_device_id(bus_id):
    """Map a CUDA bus id back to its PCI device id so non-CMP cards are skipped."""
    try:
        out = subprocess.run(["lspci", "-n", "-s", bus_id], capture_output=True,
                             text=True).stdout
    except FileNotFoundError:
        return None
    m = re.search(r"10de:([0-9a-fA-F]{4})", out)
    return m.group(1).lower() if m else None


def upstream_bridge(bus_id):
    """Upstream PCI bridge, so pairs can be reported as same-switch or not."""
    path = "/sys/bus/pci/devices/%s" % bus_id
    try:
        parent = os.path.basename(os.path.dirname(os.path.realpath(path)))
    except OSError:
        return None
    return parent if re.match(r"^[0-9a-f]{4}:", parent or "") else None


def keyed_pattern(nbytes, src, dst):
    """Pattern unique per direction: a stale or half-written buffer cannot pass."""
    seed = (0xC0FFEE ^ (src << 8) ^ dst) & 0xFFFFFFFF
    rng = random.Random(seed)
    chunk = 1 << 20
    parts = []
    remaining = nbytes
    if hasattr(rng, "randbytes"):
        # Chunked: randbytes() builds one big int internally and overflows
        # past ~256 MiB in a single call.
        while remaining > 0:
            take = min(chunk, remaining)
            parts.append(rng.randbytes(take))
            remaining -= take
        return b"".join(parts)
    # 3.8 fallback: key a 1 MiB block, then tile. Still unique per direction,
    # and the whole buffer is compared, so corruption anywhere still fails.
    block = bytearray(chunk)
    state = seed or 1
    for off in range(0, chunk, 8):
        state = (1103515245 * state + 12345) & 0xFFFFFFFF
        block[off:off + 8] = state.to_bytes(4, "little") * 2
    reps = -(-nbytes // chunk)
    return (bytes(block) * reps)[:nbytes]


def is_untouched(host_buf, nbytes, fill=0xA5):
    """True if the destination still holds only the pre-fill sentinel.

    cuMemcpyPeer can return CUDA_SUCCESS and move nothing when the cap was
    forced on but the PCIe path cannot actually carry peer traffic. That is a
    different fault from garbled data and points somewhere else, so it is
    reported separately.
    """
    got = memoryview(host_buf).cast("B")[:nbytes]
    return all(got[k] == fill for k in range(nbytes))


def first_mismatch(host_buf, want, nbytes):
    """Index of the first differing byte, or -1 if identical.

    Both sides are cast to unsigned-byte format first. A ctypes buffer's
    memoryview is format '<c' while bytes is 'B', and memoryviews whose
    formats differ compare unequal no matter what they contain - which
    silently turns every copy into a false CORRUPT.
    """
    got = memoryview(host_buf).cast("B")[:nbytes]
    ref = memoryview(want).cast("B")[:nbytes]
    if got == ref:
        return -1
    for k in range(nbytes):
        if got[k] != ref[k]:
            return k
    return -1


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--size-mb", type=int, default=256, help="buffer size per copy")
    ap.add_argument("--iters", type=int, default=5, help="timed iterations per direction")
    ap.add_argument("--debug", action="store_true",
                    help="print the libcuda symbol bound for each entry point")
    ap.add_argument("--all-gpus", action="store_true",
                    help="include non-CMP GPUs instead of only 10de:20c2/2082")
    args = ap.parse_args()

    nbytes = args.size_mb * 1024 * 1024
    cu = Cuda()
    if args.debug:
        print("libcuda bindings")
        for name in sorted(cu.bound):
            print("  %-26s -> %s" % (name, cu.bound[name]))
        print()
    cu.check(cu.cuInit(0), "cuInit")

    count = ctypes.c_int()
    cu.check(cu.cuDeviceGetCount(ctypes.byref(count)), "cuDeviceGetCount")
    if count.value < 2:
        sys.exit("error: need at least 2 GPUs, found %d" % count.value)

    devs, ctxs, info = [], [], []
    for ordinal in range(count.value):
        dev = CUdevice()
        cu.check(cu.cuDeviceGet(ctypes.byref(dev), ordinal), "cuDeviceGet")
        bus = pci_bus_id(cu, dev)
        devid = lspci_device_id(bus)
        if not args.all_gpus and devid not in CMP_DEVICE_IDS:
            print("skip  GPU%d %s (10de:%s) — not a CMP 170HX" % (ordinal, bus, devid))
            continue
        ctx = CUcontext()
        cu.check(cu.cuDevicePrimaryCtxRetain(ctypes.byref(ctx), dev),
                 "cuDevicePrimaryCtxRetain")
        devs.append(dev)
        ctxs.append(ctx)
        info.append((ordinal, bus, device_name(cu, dev), upstream_bridge(bus)))

    if len(devs) < 2:
        sys.exit("error: need at least 2 CMP 170HX GPUs, found %d "
                 "(use --all-gpus to test others)" % len(devs))

    print("\nGPUs under test")
    for i, (ordinal, bus, name, bridge) in enumerate(info):
        print("  [%d] GPU%d  %s  %s  upstream=%s" % (i, ordinal, bus, name, bridge or "?"))

    bridges = {b for _, _, _, b in info}
    if len(bridges) == 1 and None not in bridges:
        print("  topology: all cards share upstream bridge %s" % bridges.pop())
    else:
        print("  topology: cards span multiple upstream bridges %s" % sorted(
            b or "?" for b in bridges))
        print("  NOTE: the read-cap override was written for a common switch. Across")
        print("        bridges peer traffic climbs to the root complex instead, which")
        print("        can work - verified at ~2.8 GB/s on two PEX 8747s on one NUMA")
        print("        node - but is not guaranteed. The byte check below decides;")
        print("        do not infer either way from the topology alone.")

    # Peer access must be enabled per ordered pair before cuMemcpyPeer is a
    # peer copy rather than a staged one.
    # Probe each context once before doing any work. A failure here means the
    # context or its binding is wrong, and separates that from a copy that
    # genuinely broke something.
    for i in range(len(devs)):
        rc = cu.cuCtxSetCurrent(ctxs[i])
        rc2 = cu.cuCtxSynchronize()
        if args.debug or rc != CUDA_SUCCESS or rc2 != CUDA_SUCCESS:
            print("  ctx[%d] handle=0x%x setcurrent=%d synchronize=%d"
                  % (i, ctxs[i].value or 0, rc, rc2))
        if rc != CUDA_SUCCESS:
            sys.exit("error: cannot make context %d current (%d)" % (i, rc))

    print("\nEnabling peer access")
    reachable = {}
    for i in range(len(devs)):
        for j in range(len(devs)):
            if i == j:
                continue
            can = ctypes.c_int()
            cu.check(cu.cuDeviceCanAccessPeer(ctypes.byref(can), devs[i], devs[j]),
                     "cuDeviceCanAccessPeer")
            reachable[(i, j)] = bool(can.value)
            if not can.value:
                print("  [%d]->[%d] driver reports NOT peer-capable" % (i, j))
                continue
            cu.check(cu.cuCtxSetCurrent(ctxs[i]), "cuCtxSetCurrent")
            rc = cu.cuCtxEnablePeerAccess(ctxs[j], 0)
            if rc not in (CUDA_SUCCESS, CUDA_ERROR_PEER_ACCESS_ALREADY_ENABLED):
                cu.check(rc, "cuCtxEnablePeerAccess [%d]->[%d]" % (i, j))
            print("  [%d]->[%d] peer access enabled" % (i, j))

    bufs = []
    for i in range(len(devs)):
        cu.check(cu.cuCtxSetCurrent(ctxs[i]), "cuCtxSetCurrent")
        ptr = CUdeviceptr()
        cu.check(cu.cuMemAlloc(ctypes.byref(ptr), ctypes.c_size_t(nbytes)), "cuMemAlloc")
        bufs.append(ptr)

    host_out = ctypes.create_string_buffer(nbytes)
    sentinel = b"\xA5" * nbytes
    failures = []
    print("\nCopying %d MiB per direction, verifying every byte on the host" % args.size_mb)
    print("%-12s %-10s %-12s %s" % ("pair", "result", "bandwidth", "detail"))

    for i in range(len(devs)):
        for j in range(len(devs)):
            if i == j:
                continue
            label = "[%d]->[%d]" % (i, j)
            if not reachable[(i, j)]:
                print("%-12s %-10s %-12s %s" % (label, "SKIP", "-",
                                                "driver reports not peer-capable"))
                failures.append("%s not peer-capable" % label)
                continue

            pattern = keyed_pattern(nbytes, i, j)
            # Source gets the pattern; destination gets a sentinel, so a copy
            # that never lands is a failure rather than a lucky match.
            cu.check(cu.cuCtxSetCurrent(ctxs[i]), "cuCtxSetCurrent")
            cu.check(cu.cuMemcpyHtoD(bufs[i], ctypes.cast(ctypes.c_char_p(pattern),
                                                           ctypes.c_void_p),
                                     ctypes.c_size_t(nbytes)), "cuMemcpyHtoD src")
            cu.check(cu.cuCtxSetCurrent(ctxs[j]), "cuCtxSetCurrent")
            cu.check(cu.cuMemcpyHtoD(bufs[j], ctypes.cast(ctypes.c_char_p(sentinel),
                                                           ctypes.c_void_p),
                                     ctypes.c_size_t(nbytes)), "cuMemcpyHtoD dst sentinel")

            cu.check(cu.cuMemcpyPeer(bufs[j], ctxs[j], bufs[i], ctxs[i],
                                     ctypes.c_size_t(nbytes)), "cuMemcpyPeer")

            cu.check(cu.cuCtxSetCurrent(ctxs[j]), "cuCtxSetCurrent")
            cu.check(cu.cuMemcpyDtoH(ctypes.cast(host_out, ctypes.c_void_p), bufs[j],
                                     ctypes.c_size_t(nbytes)), "cuMemcpyDtoH")
            bad = first_mismatch(host_out, pattern, nbytes)
            if bad >= 0:
                if is_untouched(host_out, nbytes):
                    verdict = "NO-OP"
                    detail = ("destination untouched — the copy reported success "
                              "but moved nothing")
                else:
                    verdict = "CORRUPT"
                    got_b = memoryview(host_out).cast("B")
                    detail = "first mismatch at byte %d (got 0x%02x want 0x%02x)" % (
                        bad, got_b[bad], pattern[bad])
                print("%-12s %-10s %-12s %s" % (label, verdict, "-", detail))
                failures.append("%s %s: %s" % (label, verdict.lower(), detail))
                continue

            #
            # cuMemcpyPeer is only synchronous with respect to the host when one
            # side is host memory. Device-to-device it may return as soon as the
            # copy is enqueued, so the clock has to be closed on a synchronise or
            # this measures launch overhead and reports hundreds of GB/s over a
            # link that tops out in single digits.
            #
            cu.check(cu.cuCtxSetCurrent(ctxs[j]), "cuCtxSetCurrent")
            cu.check(cu.cuCtxSynchronize(), "cuCtxSynchronize")
            start = time.perf_counter()
            for _ in range(args.iters):
                cu.check(cu.cuMemcpyPeer(bufs[j], ctxs[j], bufs[i], ctxs[i],
                                         ctypes.c_size_t(nbytes)), "cuMemcpyPeer timed")
            cu.check(cu.cuCtxSynchronize(), "cuCtxSynchronize")
            elapsed = time.perf_counter() - start
            gbps = (nbytes * args.iters) / elapsed / 1e9
            note = "all %d bytes verified" % nbytes
            if gbps > PCIE_CEILING_GBPS:
                note += " — BANDWIDTH IMPLAUSIBLE, treat as unmeasured"
            print("%-12s %-10s %-12s %s" % (label, "OK", "%.2f GB/s" % gbps, note))

    for i in range(len(devs)):
        cu.cuCtxSetCurrent(ctxs[i])
        cu.cuMemFree(bufs[i])

    print()
    if failures:
        print("FAIL: %d of %d ordered pairs did not verify" % (
            len(failures), len(devs) * (len(devs) - 1)))
        for f in failures:
            print("  - %s" % f)
        print("\nP2P is NOT safe to rely on here. Reinstall without it:")
        print("  sudo ./install.sh   # omit --p2p, keeps the memory unlock")
        if len({b for _, _, _, b in info}) > 1:
            print("\nThe cards are behind different upstream bridges, so peer")
            print("traffic has to climb to the root complex. Two things make that")
            print("fail silently, both worth checking before giving up on P2P:")
            print("  - ACS on the switches forces peer DMA through the IOMMU.")
            print("    Separate IOMMU groups per card means it is on:")
            print("      for d in /sys/kernel/iommu_groups/*/devices/*; do echo $d; done \\")
            print("        | grep -E '$(lspci -Dn | awk \'/10de:20c2|10de:2082/{print $1}\' \\")
            print("          | paste -sd\\|)'")
            print("  - Moving the cards behind one switch removes the hop entirely")
            print("    and matches the topology the read-cap override assumes.")
        return 1

    print("PASS: every ordered pair moved correct bytes.")
    print("Cross-check the driver actually took the BAR1 path:")
    print("  sudo dmesg | grep -i CMPUNLOCK_BAR1P2P")
    print("For scale: Gen2 x16 is ~8 GB/s theoretical. Peer traffic that has to")
    print("cross the root complex has been measured at ~2.8 GB/s on two PEX 8747")
    print("switches. Well under ~0.5 GB/s is the number to be suspicious of - that")
    print("is the range a copy staged through host memory lands in.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
