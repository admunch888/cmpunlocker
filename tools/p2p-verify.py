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


class Cuda:
    def __init__(self):
        self.lib = load_libcuda()
        self.lib.cuGetErrorString.argtypes = [ctypes.c_int,
                                              ctypes.POINTER(ctypes.c_char_p)]

    def check(self, rc, what):
        if rc == CUDA_SUCCESS:
            return
        msg = ctypes.c_char_p()
        self.lib.cuGetErrorString(rc, ctypes.byref(msg))
        detail = msg.value.decode() if msg.value else "unknown"
        sys.exit("error: %s failed: %s (%d)" % (what, detail, rc))

    def __getattr__(self, name):
        return getattr(self.lib, name)


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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--size-mb", type=int, default=256, help="buffer size per copy")
    ap.add_argument("--iters", type=int, default=5, help="timed iterations per direction")
    ap.add_argument("--all-gpus", action="store_true",
                    help="include non-CMP GPUs instead of only 10de:20c2/2082")
    args = ap.parse_args()

    nbytes = args.size_mb * 1024 * 1024
    cu = Cuda()
    cu.check(cu.cuInit(0), "cuInit")

    count = ctypes.c_int()
    cu.check(cu.cuDeviceGetCount(ctypes.byref(count)), "cuDeviceGetCount")
    if count.value < 2:
        sys.exit("error: need at least 2 GPUs, found %d" % count.value)

    devs, ctxs, info = [], [], []
    for ordinal in range(count.value):
        dev = ctypes.c_int()
        cu.check(cu.cuDeviceGet(ctypes.byref(dev), ordinal), "cuDeviceGet")
        bus = pci_bus_id(cu, dev)
        devid = lspci_device_id(bus)
        if not args.all_gpus and devid not in CMP_DEVICE_IDS:
            print("skip  GPU%d %s (10de:%s) — not a CMP 170HX" % (ordinal, bus, devid))
            continue
        ctx = ctypes.c_void_p()
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
        print("  NOTE: the --p2p read-cap override assumes a common switch. Across")
        print("        bridges peer traffic may cross the host bridge, where the")
        print("        override is not safe. Treat failures below as real.")

    # Peer access must be enabled per ordered pair before cuMemcpyPeer is a
    # peer copy rather than a staged one.
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
        ptr = ctypes.c_ulonglong()
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
            cu.check(cu.cuMemcpyHtoD(bufs[i], pattern, ctypes.c_size_t(nbytes)),
                     "cuMemcpyHtoD src")
            cu.check(cu.cuCtxSetCurrent(ctxs[j]), "cuCtxSetCurrent")
            cu.check(cu.cuMemcpyHtoD(bufs[j], sentinel, ctypes.c_size_t(nbytes)),
                     "cuMemcpyHtoD dst sentinel")
            cu.check(cu.cuCtxSynchronize(), "cuCtxSynchronize")

            cu.check(cu.cuMemcpyPeer(bufs[j], ctxs[j], bufs[i], ctxs[i],
                                     ctypes.c_size_t(nbytes)), "cuMemcpyPeer")
            cu.check(cu.cuCtxSynchronize(), "cuCtxSynchronize")

            cu.check(cu.cuCtxSetCurrent(ctxs[j]), "cuCtxSetCurrent")
            cu.check(cu.cuMemcpyDtoH(host_out, bufs[j], ctypes.c_size_t(nbytes)),
                     "cuMemcpyDtoH")
            got = memoryview(host_out)[:nbytes]
            if got != memoryview(pattern):
                bad = next((k for k in range(nbytes) if got[k] != pattern[k]), -1)
                detail = "first mismatch at byte %d (got 0x%02x want 0x%02x)" % (
                    bad, got[bad], pattern[bad]) if bad >= 0 else "length mismatch"
                print("%-12s %-10s %-12s %s" % (label, "CORRUPT", "-", detail))
                failures.append("%s corrupt: %s" % (label, detail))
                continue

            start = time.perf_counter()
            for _ in range(args.iters):
                cu.check(cu.cuMemcpyPeer(bufs[j], ctxs[j], bufs[i], ctxs[i],
                                         ctypes.c_size_t(nbytes)), "cuMemcpyPeer timed")
            cu.check(cu.cuCtxSynchronize(), "cuCtxSynchronize")
            elapsed = time.perf_counter() - start
            gbps = (nbytes * args.iters) / elapsed / 1e9
            print("%-12s %-10s %-12s %s" % (label, "OK", "%.2f GB/s" % gbps,
                                            "all %d bytes verified" % nbytes))

    for i in range(len(devs)):
        cu.cuCtxSetCurrent(ctxs[i])
        cu.cuMemFree(bufs[i])

    print()
    if failures:
        print("FAIL: %d of %d ordered pairs did not verify" % (
            len(failures), len(devs) * (len(devs) - 1)))
        for f in failures:
            print("  - %s" % f)
        print("\nP2P is NOT safe to rely on. Reinstall without --p2p:")
        print("  sudo ./install.sh   # omit --p2p")
        return 1

    print("PASS: every ordered pair moved correct bytes.")
    print("Cross-check the driver actually took the BAR1 path:")
    print("  sudo dmesg | grep -i CMPUNLOCK_BAR1P2P")
    print("A Gen2 x16 link tops out near 1.4 GB/s; far below that suggests the")
    print("copy was staged through host memory rather than peer-to-peer.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
