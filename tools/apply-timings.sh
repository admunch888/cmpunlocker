#!/bin/bash
#
# cmpunlocker - apply FBPA DRAM timing overrides to every CMP 170HX.
#
# Installed to /usr/lib/cmpunlocker/apply-timings.sh and run by
# cmpunlocker-timings.service on every boot.
#
# Why a boot-time service rather than a value compiled into the driver: the
# FBPA CONFIG registers are volatile, so a reboot restores the VBIOS table.
# That makes a bad value cost one reboot instead of a reinstall and a cold
# boot. It also means an override has to be replayed after every boot to
# persist, which is what this does.
#
# The writes go through the fbpa_regs helper from overclocking/timings rather
# than through the driver: those registers are writable from the host over BAR0
# once the FBPA_MEM PLM is open, and are picked up on running traffic with no
# retrain, so this deliberately runs after the driver is up.
#
set -uo pipefail

CONF="${CMPUNLOCKER_TIMINGS_CONF:-/etc/cmpunlocker/timings.conf}"
STATE_DIR="/var/lib/cmpunlocker/state"
LOG_TAG="cmpunlocker-timings"

log()  { echo "${LOG_TAG}: $*"; }
warn() { echo "${LOG_TAG}: WARNING: $*" >&2; }

[[ -r "${CONF}" ]] || { log "no ${CONF}; nothing to apply"; exit 0; }

# fbpa_regs is not built by the driver build. Prefer an explicit path, then the
# places a clone normally leaves it.
find_tool() {
    local c
    for c in "${CMPUNLOCKER_FBPA_REGS:-}" \
             /usr/lib/cmpunlocker/fbpa_regs \
             /usr/local/bin/fbpa_regs; do
        if [[ -n "${c}" && -x "${c}" ]]; then
            echo "${c}"
            return 0
        fi
    done
    for c in /root /home/*; do
        if [[ -x "${c}/cmpunlocker/overclocking/timings/fbpa_regs" ]]; then
            echo "${c}/cmpunlocker/overclocking/timings/fbpa_regs"
            return 0
        fi
    done
    return 1
}

TOOL="$(find_tool || true)"
if [[ -z "${TOOL}" ]]; then
    warn "fbpa_regs not found - timings in ${CONF} were NOT applied"
    warn "build it with: cc -O2 -o fbpa_regs overclocking/timings/fbpa_regs.c"
    warn "then set CMPUNLOCKER_FBPA_REGS, or install it to /usr/lib/cmpunlocker/"
    exit 1
fi
log "using ${TOOL}"

mapfile -t GPUS < <("${TOOL}" list 2>/dev/null |
                    grep -oE '[0-9a-f]{4}:[0-9a-f]{2}:[0-9a-f]{2}\.[0-9a-f]' || true)
if [[ ${#GPUS[@]} -eq 0 ]]; then
    warn "fbpa_regs reports no CMP 170HX - is the driver loaded?"
    exit 1
fi

mkdir -p "${STATE_DIR}" 2>/dev/null || true
rc=0
applied=0
for bdf in "${GPUS[@]}"; do
    #
    # Snapshot once per boot before touching anything, so a bad value can be
    # put back with `fbpa_regs load` without waiting for a reboot.
    #
    snap="${STATE_DIR}/timings-${bdf}.stock"
    if [[ ! -f "${snap}" ]]; then
        "${TOOL}" -b "${bdf}" save "${snap}" >/dev/null 2>&1 || true
    fi

    while read -r line; do
        line="${line%%#*}"
        line="${line//[[:space:]]/}"
        [[ -z "${line}" ]] && continue
        field="${line%%=*}"
        value="${line#*=}"
        if [[ -z "${field}" || -z "${value}" || ! "${value}" =~ ^[0-9]+$ ]]; then
            warn "${bdf}: skipping malformed entry '${line}'"
            rc=1
            continue
        fi
        before="$("${TOOL}" -b "${bdf}" get "${field}" 2>/dev/null || echo '?')"
        if ! "${TOOL}" -b "${bdf}" set "${field}" "${value}" >/dev/null 2>&1; then
            warn "${bdf}: ${field}=${value} write failed"
            rc=1
            continue
        fi
        #
        # Read back rather than trust the exit code. Most of these fields have a
        # read-only *_GEN mirror the controller updates, so a write that did not
        # take is visible instead of assumed.
        #
        after="$("${TOOL}" -b "${bdf}" get "${field}" 2>/dev/null || echo '?')"
        if [[ "${after}" != "${value}" ]]; then
            warn "${bdf}: ${field} read back as ${after}, wanted ${value} (was ${before})"
            rc=1
            continue
        fi
        log "${bdf}: ${field} ${before} -> ${after}"
        applied=$((applied + 1))
    done < "${CONF}"
done

log "applied ${applied} override(s) across ${#GPUS[@]} GPU(s), rc=${rc}"
if (( rc != 0 )); then
    warn "at least one override did not stick; stock snapshots are in ${STATE_DIR}"
fi
exit "${rc}"
