<div align="center" style="text-align: center;">
  <img width="1280" height="300" alt="cmpunlocker banner" src="https://github.com/user-attachments/assets/6edceb8e-afcb-43a4-b5b2-4321d81284d1" />
</div>

---

## What is cmpunlocker?

<p>
  cmpunlocker restores numerous features that are restricted in firmware/OTP configuration of the NVIDIA CMP 170HX. cmpunlocker has been featured by multiple outlets like wccftech, Tom's Hardware and LinusTechTips.
</p>

<table>
  <tr>
    <td><a href="https://www.youtube.com/watch?v=pvSdeU13hKc" title=""><img src="https://github.com/user-attachments/assets/be0a9cab-19b6-47c4-91cd-cb6a98be406f"></a></td>
    <td><a href="https://www.tomshardware.com/pc-components/gpus/nvidia-crypto-mining-gpus-hacked-to-restore-locked-away-vram-in-order-to-feed-ai-boom-software-mod-unlocks-64gb-of-vram-on-usd250-cmp-170hx" title="Article by Tom's Hardware"><img src="https://github.com/user-attachments/assets/e801b2b3-3002-4346-a9ad-6b228ef62b6a"></a></td>
    <td><a href="https://wccftech.com/nvidia-cmp-170hx-8-10-gb-prices-explode-over-1000-usd-as-tool-unlocks-hidden-64-80gb-vram/" title="Article by wccftech"><img src="https://github.com/user-attachments/assets/475b5acf-999b-430c-8ede-1c39e9fd6b97"></a></td>
  </tr>
</table>

---

## Proof of Concept

Below are memory and performance results after applying the unlock:

<table>
  <tr>
    <td><b>Memory Unlock Results</b></td>
  </tr>
  <tr>
    <td><img alt="memory unlock" src="https://github.com/user-attachments/assets/ae062bd8-e3a7-4e73-b9a4-fbcde53f3c7b" width="100%" style="max-width: 900px;" /></td>
  </tr>
</table>

<table>
  <tr>
    <td><b>Performance Benchmarks (<a href="https://github.com/ProjectPhysX/OpenCL-Benchmark">OpenCL-Benchmark</a>)</b></td>
  </tr>
  <tr>
    <td><img alt="performance benchmarks" src="https://github.com/user-attachments/assets/2501506d-420f-4014-9574-b1bd0290eb60" width="100%" style="max-width: 900px;" /></td>
  </tr>
</table>

---

## About this fork

This is a fork of [amoghmunikote/cmpunlocker](https://github.com/amoghmunikote/cmpunlocker)
that adds, on top of the upstream base:

- **HBM2e clock tuning** (downclock / overclock) via `--mclk-ndiv=N`
- **PMA region fix** — the late-PMA extension is skipped on CMP cards, avoiding
  the Xid 31 `REGION_VIOLATION` faults the raw 63.5 GiB extension triggers
- **Real BAR1 P2P** — CMP-only peer-access override (ported from the bayley
  P2P work), opt-in via `--p2p`
- **Persistence across kernel updates** — patched modules are rebuilt
  automatically on kernel upgrades instead of silently falling back to stock
- **Anti-rollback** — NVIDIA driver packages are pinned to the supported
  version so a distro upgrade cannot strand the unlock

This work builds on [amoghmunikote](https://github.com/amoghmunikote/cmpunlocker),
[asm64-hooligan](https://github.com/asm64-hooligan/cmpunlocker), and
[bayley](https://github.com/bayley) — see [CREDITS.md](CREDITS.md) for the full
lineage.

---

## Requirements

- Linux (x86-64), root access
- NVIDIA CMP 170HX (GA100, `10de:20c2` 8 GB → 64 GB, `10de:2082` 10 GB → 40 GB)
- **nvidia-open already installed** (libs + firmware) at one of the versions in
  [`driver/VERSION`](driver/VERSION): 615.71.09, 610.57.04, 610.43.03, 610.43.02
- Kernel headers matching the running kernel (`linux-headers-$(uname -r)` / `kernel-devel`)
- Secure Boot disabled (patched modules are unsigned)
- Network access on first install (downloads matching stock `open-gpu-kernel-modules` sources)
- Python 3 (build-time profile selection)

---

## Install

```bash
sudo ./install.sh
```

Then perform a **cold reboot** (full power off, then boot).

### Options

```bash
sudo ./install.sh [--profile=8gb|10gb] [--no-iommu] [--no-gen2-service] \
                  [--mclk-ndiv=N] [--p2p] [--no-persist] [--no-pin] \
                  [--no-passthrough]
```

| Flag | Effect |
|------|--------|
| `--profile=8gb\|10gb` | Force a metadata label (geometry is still chosen per PCI ID) |
| `--no-iommu` | Do not touch the kernel command line |
| `--no-gen2-service` | Do not install the early-boot PCIe Gen2 retrain service |
| `--mclk-ndiv=N` | Compile an HBM2e PLL target from NDIV 30–80 (N × 27 MHz). Below the VBIOS NDIV downclocks, above it overclocks. Omit to keep the VBIOS clock. |
| `--p2p` | Enable CMP-only BAR1 peer access (see below) |
| `--no-persist` | Do not auto-rebuild patched modules after kernel updates |
| `--no-pin` | Do not pin the installed NVIDIA driver packages |
| `--no-passthrough` | Do not prepare the cards for VM passthrough (on by default, so a VM sees an unlocked card with a stock driver) |

### HBM2e clock tuning (`--mclk-ndiv`)

The HBM2e PLL target is set from `N × 27 MHz`. The VBIOS NDIV is stock; lower
values downclock (less power, cooler HBM), higher values overclock (more
bandwidth, more heat). **Qualify an overclock before baking it in** — the value
is compiled into the driver and has no runtime safety net:

```bash
sudo WORKLOAD_TIMEOUT=28800 170tune hbm-gate --ndiv 70 --sweeps 12 \
     --workload /usr/local/bin/vllm_workload_check.sh
```

Only pass `--mclk-ndiv=70` once the gate passes. If it fails, step down to
66/68 (lower = safer). Omitting the flag preserves the stock VBIOS clock, which
is the verified-good state.

### DRAM timings (`--refresh=N`)

Sets the FBPA **`REFRESH`** field (`CONFIG4`) on every card — the field
`fbpa_regs` reports by that name, not `RFC`/tRFC in `CONFIG0`. Stock on a
CMP 170HX 8GB is **6**; `RFC` is a separate field at **657** cycles and the
controller rejects out-of-range writes to it.

This is **not** compiled into the driver. The FBPA `CONFIG0..CONFIG4`
registers are volatile, so a reboot restores the VBIOS table — a bad value
costs one reboot rather than a reinstall and a cold boot. The value is stored
in `/etc/cmpunlocker/timings.conf` and replayed at each boot by
`cmpunlocker-timings.service`, which runs *after* the driver is up because the
`FBPA_MEM` PLM only opens on the first Booter round trip.

```bash
sudo ./install.sh --mclk-ndiv=70 --refresh=24
# after reboot
fbpa_regs get REFRESH
systemctl status cmpunlocker-timings
```

Needs the `fbpa_regs` helper from `overclocking/timings`; `install.sh` builds
it if the sources are present, and the service reports loudly if it is missing
rather than skipping silently.

**Refresh governs how often DRAM cells are topped up**, so changing it trades
retention margin for bandwidth, and the margin also moves with `--mclk-ndiv`
because these fields hold cycles rather than nanoseconds. Read the stock value
before changing it (`fbpa_regs get REFRESH`), and validate with
`gpu_burn` reporting **zero** errors — a too-low `tRFC` does not fail loudly,
it loses charge in DRAM cells and returns wrong data. `overclocking/timings/`
has the measured sensitivity of every field; on this card bandwidth is
clock-bound rather than timing-bound, and `tCCD_L=3` wedges the memory
controller unrecoverably.

To revert: reinstall without `--refresh` (or `remove.sh`), then reboot.

### P2P (`--p2p`)

Opt-in. Enables real BAR1 peer access for CMP 170HX/220HX by overriding the GSP
firmware's PCIe peer-capability report, forcing the BAR1 P2P path, and skipping
the mailbox peer pre-registration that otherwise blocks it. Requires a
compatible PCIe topology (both cards behind a common switch).

**Do not trust `nvidia-smi topo -p2p` or `torch.cuda.can_device_access_peer()`
alone** — those report what the driver claims, and `--p2p` works by changing
exactly that claim. Verify with a real peer-to-peer copy that checks the
destination bytes in both directions:

```bash
sudo ./tools/p2p-verify.py            # add --size-mb / --iters to taste
sudo dmesg | grep -i CMPUNLOCK_BAR1P2P
```

`tools/p2p-verify.py` talks to `libcuda` directly, so it needs no CUDA toolkit
and no PyTorch. It copies a per-direction keyed pattern between every ordered
GPU pair, reads the destination back, and exits non-zero if any byte differs.

**Verified configuration** (2026-09-13): 2x CMP 170HX 8GB on nvidia-open
615.71.09, kernel 7.0.0-31, behind *two different* PLX PEX 8747 switches
(`05:10.0` and `09:08.0`) on one NUMA node — **2.85 GB/s both directions**,
268 MB verified per pair. A common switch is not required; peer traffic
crossing the root complex works here.

`NVreg_RegistryDwords` must actually be live for this to work, and that is not
visible from `/etc/modprobe.d` alone — the driver loads from the initramfs:

```bash
grep -i registrydwords /proc/driver/nvidia/params
# RegistryDwords: "RMForceStaticBar1=1;RMPcieP2PType=1;RmForceEnableGen2=1;RMPcieLinkSpeed=0x1"
```

An empty string there means the options never reached the driver. Without
`RMForceStaticBar1` the BAR1 mapping gate declines while the forced read cap
still advertises peer access, so `cuMemcpyPeer` returns success and transfers
nothing. Run `sudo update-initramfs -u` and cold boot.

### PMA region fix (always on)

The late-PMA extension is skipped on CMP cards. Publishing the highest reserved
FB region hands the page allocator memory that overlaps the write-protected
region and the GSP heap, and the hardware refuses copy-engine writes there
(Xid 31 `REGION_VIOLATION` a few seconds into a large weight load). Skipping
costs ~141 MB of the 63.5 GiB exposed memory and removes the fault entirely.

---

## What Gets Unlocked

<table>
  <tr>
    <th>Feature</th>
    <th>Status</th>
  </tr>
  <tr>
    <td>Full SM compute throughput (SS0/SS1)</td>
    <td>Working ✓</td>
  </tr>
  <tr>
    <td>Memory geometry (64GB on 8GB cards, 40GB on 10GB cards)</td>
    <td>Working ✓</td>
  </tr>
  <tr>
    <td>PCIe Gen 2 speeds</td>
    <td>Working ✓</td>
  </tr>
  <tr>
    <td>Full BAR1 Size (64GB)</td>
    <td>Working ✓</td>
  </tr>
  <tr>
    <td>JTAG (Host2Jtag register access)</td>
    <td>Working ✓</td>
  </tr>
  <tr>
    <td>VFIO-based passthrough</td>
    <td>Working ✓</td>
  </tr>
  <tr>
    <td>GPU profiling</td>
    <td>Working ✓</td>
  </tr>
  <tr>
    <td>Persistence across reboot (patched modules)</td>
    <td>Working ✓</td>
  </tr>
  <tr>
    <td>HBM2e clock tuning (downclock / overclock)</td>
    <td>Working ✓ (<code>--mclk-ndiv</code>)</td>
  </tr>
  <tr>
    <td>BAR1 P2P</td>
    <td>Working ✓ (<code>--p2p</code>)</td>
  </tr>
  <tr>
    <td>Persistence across kernel updates</td>
    <td>Working ✓ (on by default)</td>
  </tr>
  <tr>
    <td>Anti-rollback (driver pin)</td>
    <td>Working ✓ (on by default)</td>
  </tr>
</table>

---

## Surviving kernel updates (on by default)

A kernel update rebuilds the patched modules through a package-manager hook
(`/etc/kernel/install.d`, `/etc/kernel/postinst.d`, or a pacman hook) before you
reboot. `cmpunlocker-rebuild.service` is the safety net for anything the hook
misses — it holds boot until the modules exist rather than letting the card
come up unpatched at 8 GB.

The NVIDIA driver packages are pinned to their installed version. A driver
upgrade past the versions in `driver/VERSION` would make every later rebuild
fail and drop the card back to stock; the pin prevents that. GPU *firmware*
packages are deliberately left unpinned (they belong to `linux-firmware` and
holding them can wedge upgrades).

Check state with:

```bash
systemctl status cmpunlocker-rebuild
sudo /usr/lib/cmpunlocker/pin-packages.sh status
cat /var/log/cmpunlocker/rebuild-$(uname -r).log
```

Opt out with `--no-persist` / `--no-pin`.

---

## Verify

```bash
sudo ./verify.sh
nvidia-smi --query-gpu=driver_version,index,name,memory.total --format=csv
nvidia-smi --query-gpu=pcie.link.gen.current,pcie.link.gen.max --format=csv
```

Expect 2× `CMP 170HX` at ~65536 MiB and PCIe `2,2`.

---

## Uninstall

```bash
sudo ./remove.sh --yes
```

Then cold reboot. This removes the patched modules, undoes the kernel-update
hooks, releases the package pin, and restores the pre-install kernel command
line. The driver already running in memory is left alone — the card comes up on
the stock driver at the next boot, which is the safe order.

`--reload` swaps the running driver immediately instead of waiting for the
reboot. It is off by default because loading the stock `nvidia-drm` against a
CMP can wedge the machine.

---

## Contributions

Please read [docs/CONTRIBUTING.md](https://github.com/amoghmunikote/cmpunlocker/blob/master/docs/CONTRIBUTING.md) before opening a PR.

## Support & Community

Join our [Discord community](https://discord.gg/CdHSakKSFv) to discuss with
other users and get support.
