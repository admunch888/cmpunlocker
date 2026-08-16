# cmpunlocker

Unlock tool for the NVIDIA CMP 170HX (GA100) mining card. Restores full SM
compute throughput and unlocked HBM2e memory geometry that are restricted in
firmware/OTP configuration.

This is a fork of [amoghmunikote/cmpunlocker](https://github.com/amoghmunikote/cmpunlocker)
that adds the missing features on top of the 610.57.04-compatible base:

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

**[Join the Discord community](https://discord.gg/CdHSakKSFv)** for support.

---

## Requirements

- Linux (x86-64), root access
- NVIDIA CMP 170HX (GA100, `10de:20c2` 8 GB → 64 GB, `10de:2082` 10 GB → 40 GB)
- **nvidia-open 610.57.04 (or 610.43.03/610.43.02) already installed** (libs + firmware)
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
                  [--mclk-ndiv=N] [--p2p] [--no-persist] [--no-pin]
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

### P2P (`--p2p`)

Opt-in. Enables real BAR1 peer access for CMP 170HX/220HX by overriding the GSP
firmware's PCIe peer-capability report, forcing the BAR1 P2P path, and skipping
the mailbox peer pre-registration that otherwise blocks it. Requires a
compatible PCIe topology (both cards behind a common switch).

**Do not trust `nvidia-smi topo -p2p` or `torch.cuda.can_device_access_peer()`
alone** — verify with a real peer-to-peer copy that checks the destination
bytes in both directions. See `benchmark/` for a test.

### PMA region fix (always on)

The late-PMA extension is skipped on CMP cards. Publishing the highest reserved
FB region hands the page allocator memory that overlaps the write-protected
region and the GSP heap, and the hardware refuses copy-engine writes there
(Xid 31 `REGION_VIOLATION` a few seconds into a large weight load). Skipping
costs ~141 MB of the 63.5 GiB exposed memory and removes the fault entirely.

---

## What Gets Unlocked

| Feature | Status |
|---|---|
| Full SM compute throughput (SS0/SS1) | Working ✓ |
| Memory geometry (64 GB on 8 GB cards, 40 GB on 10 GB cards) | Working ✓ |
| PCIe Gen 2 speeds | Working ✓ |
| Full BAR1 Size (64 GB) | Working ✓ |
| JTAG (Host2Jtag register access) | Working ✓ |
| HBM2e clock tuning (downclock / overclock) | Working ✓ (`--mclk-ndiv`) |
| BAR1 P2P | Working ✓ (`--p2p`) |
| Persistence across kernel updates | Working ✓ (on by default) |
| Anti-rollback (driver pin) | Working ✓ (on by default) |

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

## Support & Community

Join our [Discord community](https://discord.gg/CdHSakKSFv) to discuss with
other users and get support.
