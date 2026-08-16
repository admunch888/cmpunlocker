#!/bin/bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

source "${SCRIPT_DIR}/common/lib.sh"

RELOAD_DRIVER=0
for arg in "$@"; do
    case "${arg}" in
        --reload) RELOAD_DRIVER=1 ;;
        --yes|-y) : ;;
        -h|--help)
            cat <<'EOF'
Usage: sudo ./remove.sh [--yes] [--reload]

Removes cmpunlocker from the system:
  - Stops and removes the early-boot Gen2 retrain service
  - Removes /lib/modules/*/updates/cmpunlocker/
  - Removes the kernel-update hooks, the boot-time rebuild service, and
    the NVIDIA package version pin
  - Restores the pre-install kernel command line (reverts IOMMU changes)
  - Rebuilds initramfs

By default the driver already running in memory is left alone. The card comes
up on the stock driver at the next boot, which is the safe order.

  --reload  Swap the running driver for the stock one immediately instead of
            waiting for the reboot. OFF by default because loading the stock
            nvidia-drm against a CMP 170HX can wedge the machine (the kernel
            keeps answering pings while userspace stops making progress).

Logs under /var/log/cmpunlocker/ are kept.

Run: sudo ./remove.sh --yes
EOF
            exit 0
            ;;
        *)
            echo "Unknown argument: ${arg}" >&2
            echo "Run: sudo ./remove.sh --help" >&2
            exit 1
            ;;
    esac
done

if [[ "${1:-}" != "--yes" && "${1:-}" != "-y" ]]; then
    echo "This removes cmpunlocker patched kernel modules and all of its"
    echo "kernel-update automation. Run with --yes to proceed:"
    echo ""
    echo "  sudo ./remove.sh --yes"
    echo ""
    exit 1
fi

banner
step_init 6

step "Verifying root privileges"
[[ "${EUID}" -eq 0 ]] || die "Run as root: sudo ./remove.sh --yes"
ok "Running as root"

LOG_DIR="${SCRIPT_DIR}/logs"
if ! mkdir -p "${LOG_DIR}" 2>/dev/null || [[ ! -w "${LOG_DIR}" ]]; then
    LOG_DIR="/tmp"
fi
LOG_FILE="${LOG_DIR}/remove_$(date +%Y%m%d_%H%M%S).log"
exec > >(tee -a "${LOG_FILE}") 2>&1

step "Releasing the NVIDIA package version pin"
# Unpin first: the helper that knows how to undo the hold lives in the
# directory removed a few lines further down.
if [[ -x /usr/lib/cmpunlocker/pin-packages.sh ]]; then
    /usr/lib/cmpunlocker/pin-packages.sh unpin || true
    ok "Package pin released (if any was set)"
else
    info "No pin helper found — nothing to release"
fi

step "Removing kernel-update persistence"
if command -v systemctl &>/dev/null; then
    systemctl disable --now cmpunlocker-rebuild.service 2>/dev/null || true
    systemctl unmask nvidia-fallback.service 2>/dev/null || true
fi
rm -f /etc/systemd/system/cmpunlocker-rebuild.service
rm -f /etc/kernel/install.d/95-cmpunlocker.install
rm -f /etc/kernel/postinst.d/cmpunlocker
rm -f /etc/kernel/postrm.d/cmpunlocker
rm -f /etc/pacman.d/hooks/95-cmpunlocker.hook
rm -f /etc/depmod.d/cmpunlocker.conf
rm -f /etc/modprobe.d/cmpunlocker.conf
rm -f /etc/modprobe.d/cmp-unlock.conf
rm -rf /usr/lib/cmpunlocker
rm -rf /var/lib/cmpunlocker
rm -rf /etc/cmpunlocker
if [[ -f /etc/pacman.conf.cmpunlocker.bak ]]; then
    mv -f /etc/pacman.conf.cmpunlocker.bak /etc/pacman.conf
    ok "Restored /etc/pacman.conf from pre-install backup"
fi
command -v systemctl &>/dev/null && systemctl daemon-reload 2>/dev/null || true
ok "Kernel hooks, boot service and package pins removed"

step "Removing PCIe Gen2 helpers"
for legacy_unit in cmpretrain.service cmp-gen2-retrain.service; do
    systemctl disable --now "${legacy_unit}" 2>/dev/null || true
    systemctl reset-failed "${legacy_unit}" 2>/dev/null || true
done
rm -f /etc/systemd/system/cmpretrain.service /usr/local/sbin/retrain.sh
rm -f /etc/systemd/system/cmp-gen2-retrain.service /usr/local/sbin/cmp-gen2-retrain.sh
rm -f /etc/modprobe.d/cmp-pcie-gen2.conf
systemctl disable --now gen2.service 2>/dev/null || true
systemctl reset-failed gen2.service 2>/dev/null || true
rm -f /etc/systemd/system/gen2.service /usr/local/sbin/gen2-hammer
systemctl daemon-reload 2>/dev/null || true
ok "Removed PCIe Gen2 helpers"

step "Restoring IOMMU kernel command line"
iommu_restored=0
for cfg in /etc/default/grub /etc/kernel/cmdline; do
    if [[ -f "${cfg}.cmpunlocker.bak" ]]; then
        mv -f "${cfg}.cmpunlocker.bak" "${cfg}"
        ok "Restored ${cfg} from pre-install backup"
        iommu_restored=1
    fi
done
if (( iommu_restored )); then
    if command -v update-grub &>/dev/null; then
        update-grub 2>/dev/null || true
    elif command -v grub2-mkconfig &>/dev/null; then
        grub2-mkconfig -o /boot/grub2/grub.cfg 2>/dev/null || true
    elif command -v grub-mkconfig &>/dev/null; then
        grub-mkconfig -o /boot/grub/grub.cfg 2>/dev/null || true
    fi
    ok "Reverted IOMMU kernel parameters (effective after reboot)"
else
    warn "No IOMMU config backup found — kernel command line left as-is"
fi

step "Removing patched modules"
mod_removed=0
kernels_touched=()
shopt -s nullglob
for mod_dir in /lib/modules/*/updates/cmpunlocker; do
    if [[ -d "${mod_dir}" ]]; then
        kernel="$(basename "$(dirname "$(dirname "${mod_dir}")")")"
        rm -rf "${mod_dir}"
        # depmod AND sync: a power cut between depmod and the next flush
        # leaves a zero-length modules.dep and nothing resolves on boot.
        depmod -a "${kernel}" 2>/dev/null || true
        sync
        ok "Removed patched modules for kernel ${kernel}"
        mod_removed=$((mod_removed + 1))
        kernels_touched+=("${kernel}")
    fi
done
[[ "${mod_removed}" -gt 0 ]] || warn "No patched kernel modules found"

if [[ ${#kernels_touched[@]} -gt 0 ]]; then
    info "Rebuilding initramfs so stock modules are packed again..."
    for kernel in "${kernels_touched[@]}"; do
        if command -v update-initramfs &>/dev/null; then
            update-initramfs -u -k "${kernel}" 2>/dev/null || true
        elif command -v dracut &>/dev/null; then
            dracut --force --kver "${kernel}" 2>/dev/null || true
        fi
    done
    if command -v mkinitcpio &>/dev/null && ! command -v update-initramfs &>/dev/null && ! command -v dracut &>/dev/null; then
        mkinitcpio -P 2>/dev/null || true
    fi
    ok "initramfs rebuild attempted"
fi

for gsp in /lib/firmware/nvidia/*/gsp_tu10x.bin; do
    rm -f \
        "${gsp}.cmpunlocker.bak" \
        "${gsp}.cmpunlocker.patched" \
        "${gsp}.cmpunlocker.tmp" \
        "${gsp}.cmpunlocker.cleanup" \
        "${gsp}.cmpunlocker.pat"
done

if [[ -d /opt/cmpunlocker ]]; then
    rm -rf /opt/cmpunlocker
    ok "Removed /opt/cmpunlocker (legacy install dir)"
fi

step "Done"
banner
echo "cmpunlocker has been removed from system."
echo "Log saved to: ${LOG_FILE}"
echo ""

if (( RELOAD_DRIVER == 1 )); then
    info "Reloading stock NVIDIA driver (--reload)..."
    # Deliberately guarded with timeouts: loading stock nvidia-drm against a
    # CMP 170HX can wedge the machine (no usable display engine).
    systemctl stop nvidia-persistenced 2>/dev/null || true
    for mod in nvidia_drm nvidia_uvm nvidia_modeset nvidia; do
        timeout 20 modprobe -r "${mod}" 2>/dev/null || true
    done
    sleep 1
    if timeout 20 modprobe nvidia 2>/dev/null; then
        modprobe nvidia-modeset 2>/dev/null || true
        modprobe nvidia-uvm 2>/dev/null || true
        modprobe nvidia-drm 2>/dev/null || true
        ok "Stock NVIDIA driver reloaded"
    else
        warn "Could not reload NVIDIA driver — reboot to finish cleanup"
    fi
else
    info "Running driver left in place; the stock driver loads on next boot."
fi

echo ""
echo "Reboot once to finish:"
echo -e "  ${CYAN}sudo shutdown -h now${NC}   # cold reboot (full power off, then on)"
echo ""
