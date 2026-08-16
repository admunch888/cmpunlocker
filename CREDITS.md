# Credits

This fork builds on excellent prior work. Thanks to:

| Person | Contribution |
|---|---|
| [amoghmunikote](https://github.com/amoghmunikote/cmpunlocker) | Base fork: CMP 170HX unlock on nvidia-open 610.57.04 (JTAG, PCIe Gen 2, DKMS removal, docs) |
| [asm64-hooligan](https://github.com/asm64-hooligan/cmpunlocker) | Full BAR1 size (64 GB); HBM2e clock tuning (`--mclk-ndiv`); PMA region fix (skip late-PMA to avoid Xid 31 `REGION_VIOLATION`) |
| [bayley](https://github.com/bayley) | Real BAR1 P2P peer-access override (ported as the `--p2p` feature) |

## Lineage

```
upstream amoghmunikote/cmpunlocker   (610.57.04-compatible base)
        +
asm64-hooligan/cmpunlocker            (BAR1 64 GB, HBM tuning, PMA fix)
        +
bayley P2P work                       (BAR1 peer-access override)
        +
admunch888/cmpunlocker  (this fork)   (merge + kernel-update persistence + anti-rollback)
```

This fork (`admunch888/cmpunlocker`) merges the above and adds the kernel-update
persistence layer (auto-rebuild on kernel upgrades) and anti-rollback (driver
package pinning) so the unlock survives distro upgrades.
