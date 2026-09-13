"""
apply-timings.sh writes DRAM timing registers, so its config parsing and its
read-back check are the parts that must not be loose: a malformed line must be
skipped rather than guessed at, and a write that does not stick must be
reported rather than assumed to have worked.

fbpa_regs is stubbed, so these run anywhere - no GPU and no BAR0 access.
"""
import os
import subprocess
import textwrap

import pytest

import repo

SCRIPT = repo.ROOT / "tools" / "apply-timings.sh"
UNIT = repo.ROOT / "systemd" / "cmpunlocker-timings.service"
BDF = "0000:07:00.0"


def make_stub(tmp_path, *, applied, readback=None, gpus=(BDF,)):
    """A fake fbpa_regs. `applied` collects set calls; `readback` overrides
    what get returns, to simulate a write that did not take."""
    stub = tmp_path / "fbpa_regs"
    stub.write_text(textwrap.dedent("""\
        #!/bin/bash
        STATE="%s"
        APPLIED="%s"
        READBACK="%s"
        args=()
        while [[ $# -gt 0 ]]; do
            case "$1" in
                -b) shift 2 ;;
                -g) shift 2 ;;
                *) args+=("$1"); shift ;;
            esac
        done
        case "${args[0]}" in
            list) printf '%%s\\n' %s ;;
            get)  if [[ -n "${READBACK}" ]]; then echo "${READBACK}"
                  elif [[ -f "${STATE}/${args[1]}" ]]; then cat "${STATE}/${args[1]}"
                  else echo 512; fi ;;
            set)  echo "${args[1]}=${args[2]}" >> "${APPLIED}"
                  mkdir -p "${STATE}"; echo "${args[2]}" > "${STATE}/${args[1]}" ;;
            save) : > "${args[1]}" ;;
        esac
        exit 0
        """) % (tmp_path / "state", applied, readback or "",
                " ".join("'%s'" % g for g in gpus)))
    stub.chmod(0o755)
    return stub


def run(tmp_path, conf_text, **kw):
    conf = tmp_path / "timings.conf"
    conf.write_text(conf_text)
    applied = tmp_path / "applied"
    applied.write_text("")
    stub = make_stub(tmp_path, applied=applied, **kw)
    env = dict(os.environ,
               CMPUNLOCKER_TIMINGS_CONF=str(conf),
               CMPUNLOCKER_FBPA_REGS=str(stub))
    proc = subprocess.run(["bash", str(SCRIPT)], capture_output=True,
                          text=True, env=env)
    return proc, applied.read_text().split()


@pytest.mark.parametrize("conf,expected", [
    ("RFC=24\n", ["RFC=24"]),
    ("# comment only\n", []),
    ("\n\n", []),
    ("  RFC = 24  \n", ["RFC=24"]),
    ("RFC=24\nRP=30\n", ["RFC=24", "RP=30"]),
    ("RFC=24  # trailing comment\n", ["RFC=24"]),
])
def test_config_parsing(tmp_path, conf, expected):
    proc, applied = run(tmp_path, conf)
    assert applied == expected, proc.stdout + proc.stderr


@pytest.mark.parametrize("bad", ["RFC=\n", "=24\n", "RFC=abc\n", "RFC=-5\n"])
def test_malformed_entries_are_skipped_not_guessed(tmp_path, bad):
    proc, applied = run(tmp_path, bad)
    assert applied == []
    assert proc.returncode != 0
    assert "malformed" in (proc.stdout + proc.stderr)


def test_write_that_does_not_stick_is_reported(tmp_path):
    # readback forced to a different value than requested
    proc, applied = run(tmp_path, "RFC=24\n", readback="512")
    assert applied == ["RFC=24"]
    assert proc.returncode != 0
    assert "read back as 512" in (proc.stdout + proc.stderr)


def test_missing_config_is_not_an_error(tmp_path):
    stub = make_stub(tmp_path, applied=tmp_path / "applied")
    env = dict(os.environ,
               CMPUNLOCKER_TIMINGS_CONF=str(tmp_path / "absent.conf"),
               CMPUNLOCKER_FBPA_REGS=str(stub))
    proc = subprocess.run(["bash", str(SCRIPT)], capture_output=True,
                          text=True, env=env)
    assert proc.returncode == 0
    assert "nothing to apply" in proc.stdout


def test_every_gpu_is_covered(tmp_path):
    proc, applied = run(tmp_path, "RFC=24\n",
                        gpus=("0000:07:00.0", "0000:0a:00.0"))
    assert applied == ["RFC=24", "RFC=24"], proc.stdout + proc.stderr


def test_unit_runs_after_the_driver_not_before():
    # The FBPA_MEM PLM only opens on the driver's first Booter round trip, so
    # this unit must not be ordered before basic.target the way the Gen2
    # retrain is.
    text = UNIT.read_text()
    assert "After=multi-user.target" in text
    assert "Before=" not in text
    assert "ConditionPathExists=/etc/cmpunlocker/timings.conf" in text
