#!/bin/bash
#
# cmpunlocker - rebuild the patched modules for a given kernel.
#
# Installed to /usr/lib/cmpunlocker/rebuild.sh. Every persistence path calls
# this and nothing else: the kernel-install hook (Fedora), the postinst.d hook
# (Debian/Ubuntu/HiveOS), the pacman hook (Arch), and the boot-time service.
#
# Usage: rebuild.sh [kernel-version]   (default: running kernel)
#
# Exits 0 even when the build fails. A package manager hook that returns
# non-zero turns a driver problem into a failed system upgrade, which is worse
# than booting on the stock driver. The failure is recorded in the marker file
# so cmpunlocker-rebuild.service retries on the next boot, where it is visible.
#
set -uo pipefail

PAYLOAD_DIR="/var/lib/cmpunlocker"
CONF_FILE="/etc/cmpunlocker/build.conf"
LOG_DIR="/var/log/cmpunlocker"
MARKER_DIR="/var/lib/cmpunlocker/state"

KVER="${1:-$(uname -r)}"
LOG_FILE="${LOG_DIR}/rebuild-${KVER}.log"
MARKER="${MARKER_DIR}/failed-${KVER}"

mkdir -p "${LOG_DIR}" "${MARKER_DIR}" 2>/dev/null || true

log() { echo "cmpunlocker: $*"; }

#
# Everything below is also written to the log, so a failure that scrolled past
# during a dnf upgrade can still be read afterwards.
#
exec > >(tee -a "${LOG_FILE}") 2>&1

echo "=== $(date -Is) rebuild for ${KVER} ==="

fail() {
    log "ERROR: $*"
    log "Patched modules for ${KVER} were NOT built."
    log "Existing modules for other kernels are untouched."
    log "Log: ${LOG_FILE}"
    : > "${MARKER}"
    exit 0
}

[[ "${EUID}" -eq 0 ]] || fail "must run as root"

#
# The payload is a copy of driver/ taken at install time. Rebuilding from it
# rather than from the user's git clone means the clone can be moved or deleted
# without breaking kernel updates.
#
[[ -x "${PAYLOAD_DIR}/driver/build.sh" ]] || \
    fail "payload missing at ${PAYLOAD_DIR}/driver — re-run install.sh"

#
# The compile-time flags (overclock, timings, P2P, driver version) live here.
# Without them a rebuild would silently produce a stock-clocked driver.
#
if [[ -r "${CONF_FILE}" ]]; then
    # shellcheck disable=SC1090
    . "${CONF_FILE}"
else
    log "WARN: ${CONF_FILE} missing — building with defaults (no overclock)"
fi

[[ -d "/lib/modules/${KVER}" ]] || fail "no module tree for ${KVER}"

#
# Headers are the one thing that is genuinely not ready sometimes: on Debian
# the postinst.d hook can run before linux-headers-<ver> is unpacked. Leave the
# marker and let the boot service pick it up once the headers are in place.
#
if [[ ! -d "/lib/modules/${KVER}/build" ]]; then
    fail "kernel headers for ${KVER} not installed yet (looked in /lib/modules/${KVER}/build)"
fi

log "building for ${KVER} (running: $(uname -r))"
[[ -n "${CMPUNLOCKER_MCLK_NDIV:-}" ]]    && log "  mclk-ndiv=${CMPUNLOCKER_MCLK_NDIV}"
[[ -n "${CMPUNLOCKER_MCLK_TIMINGS:-}" ]] && log "  mclk-timings=${CMPUNLOCKER_MCLK_TIMINGS}"
[[ -n "${CMPUNLOCKER_ENABLE_P2P:-}" ]]   && log "  p2p=on"

CMPUNLOCKER_KVER="${KVER}" \
CMPUNLOCKER_DRIVER_VERSION="${CMPUNLOCKER_DRIVER_VERSION:-}" \
CMPUNLOCKER_CARD_PROFILE="${CMPUNLOCKER_CARD_PROFILE:-8gb}" \
CMPUNLOCKER_GPU_INVENTORY="$(cat /etc/cmpunlocker/gpu_inventory 2>/dev/null || true)" \
CMPUNLOCKER_MCLK_NDIV="${CMPUNLOCKER_MCLK_NDIV:-}" \
CMPUNLOCKER_MCLK_TIMINGS="${CMPUNLOCKER_MCLK_TIMINGS:-}" \
CMPUNLOCKER_ENABLE_P2P="${CMPUNLOCKER_ENABLE_P2P:-}" \
CMPUNLOCKER_VERBOSE="${CMPUNLOCKER_VERBOSE:-0}" \
    "${PAYLOAD_DIR}/driver/build.sh" || fail "build failed for ${KVER}"

MOD_DIR="/lib/modules/${KVER}/updates/cmpunlocker"
if [[ ! -f "${MOD_DIR}/nvidia.ko" ]]; then
    fail "build reported success but nvidia.ko is missing for ${KVER}"
fi

#
# Confirm the rebuild carried the options forward, rather than assuming it did.
# build.sh stamps what it actually built into the module directory, so a
# rebuild that silently dropped the overclock or P2P - an empty build.conf, a
# stale payload, a flag that stopped being honoured - is visible here instead
# of at the next boot as a card quietly running at stock.
#
want_ndiv="${CMPUNLOCKER_MCLK_NDIV:-stock}"
[[ -z "${want_ndiv}" ]] && want_ndiv="stock"
want_p2p=""
[[ -n "${CMPUNLOCKER_ENABLE_P2P:-}" ]] && want_p2p="enabled"

drift=""
check_marker() {
    local name="$1" want="$2" got=""
    [[ -r "${MOD_DIR}/${name}" ]] && got="$(tr -d '[:space:]' < "${MOD_DIR}/${name}")"
    if [[ "${got}" != "${want}" ]]; then
        log "WARN: ${name} is '${got:-<empty>}' but build.conf asked for '${want:-<none>}'"
        drift="yes"
    fi
}
check_marker mclk_ndiv "${want_ndiv}"
check_marker p2p       "${want_p2p}"
[[ -n "${CMPUNLOCKER_DRIVER_VERSION:-}" ]] && \
    check_marker driver_version "${CMPUNLOCKER_DRIVER_VERSION}"

if [[ -n "${drift}" ]]; then
    fail "rebuilt modules do not match ${CONF_FILE} (see the mismatches above)"
fi
log "verified: mclk=${want_ndiv} p2p=${want_p2p:-off}"

rm -f "${MARKER}"
log "OK: patched modules installed for ${KVER}"
exit 0
