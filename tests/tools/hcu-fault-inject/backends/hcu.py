# Copyright (c) 2026 Hygon Information Technology Co., Ltd.
# SPDX-License-Identifier: Apache-2.0

"""Inject a reversible fault on a device selected by its hy-smi number."""

from contextlib import contextmanager
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess


PCI_DEVICES = Path("/sys/bus/pci/devices")
HY_SMI_PATH = "/opt/dtk/.hyhal/bin:/opt/hyhal/bin:/opt/dtk/bin:/opt/rocm/bin"
BUS_LINE = re.compile(
    r"\b(?:HCU|DCU|GPU)\s*\[\s*(\d+)\s*\].*?\b"
    r"((?:[0-9a-f]{4}:)?[0-9a-f]{2}:[0-9a-f]{2}\.[0-7])\b",
    re.IGNORECASE,
)
CLOCK_LEVEL = re.compile(r"^\s*(\d+):\s*(\d+)\s*MHz\s*(\*)?\s*$", re.IGNORECASE)
CLOCK_MODES = {"clock-low": "low", "clock-high": "high", "clock-pulse": "low"}
RESTORABLE_MODES = {"auto", "low", "high", "profile_standard", "profile_min_sclk", "profile_peak"}
POWER_PROFILES = {"power-low", "power-pulse"}
PULSE_PROFILES = {"clock-pulse", "power-pulse"}
OPERATION_PROFILES = {"hcu-op-delay", "hcu-op-error"}
PULSE_SECONDS = 2
MICROWATTS_PER_WATT = 1_000_000


def devices():
    """Return the HCU numbers and PCI addresses displayed by hy-smi."""
    program = shutil.which("hy-smi") or shutil.which("hy-smi", path=HY_SMI_PATH)
    if program is None:
        raise ValueError("hy-smi is required to map HCU numbers to PCI addresses")
    library_dir = Path(program).resolve().parent.parent / "rocm_smi" / "lib"
    env = None
    if (library_dir / "librocm_smi64.so.2").is_file():
        env = os.environ.copy()
        configured = env.get("LD_LIBRARY_PATH")
        env["LD_LIBRARY_PATH"] = f"{configured}:{library_dir}" if configured else str(library_dir)
    try:
        result = subprocess.run([program, "--showbus"], capture_output=True, text=True,
                                encoding="utf-8", errors="replace", timeout=5, check=False, env=env)
    except subprocess.TimeoutExpired:
        raise ValueError("hy-smi --showbus timed out") from None
    if result.returncode:
        detail = result.stderr.strip()[-300:]
        raise ValueError(f"hy-smi --showbus failed with exit code {result.returncode}"
                         + (f": {detail}" if detail else ""))
    mapping = {}
    for line in result.stdout.splitlines():
        match = BUS_LINE.search(line)
        if match is None:
            continue
        number = int(match[1])
        bdf = match[2].lower()
        if len(bdf) == 7:
            bdf = f"0000:{bdf}"
        if number in mapping and mapping[number] != bdf:
            raise ValueError(f"hy-smi reports multiple PCI addresses for HCU {number}")
        mapping[number] = bdf
    if not mapping or len(set(mapping.values())) != len(mapping):
        raise ValueError("hy-smi did not report unique HCU numbers and PCI addresses")
    return [{"hcu": number, "bdf": bdf} for number, bdf in sorted(mapping.items())]


def _identity(info):
    return [info.st_dev, info.st_ino]


def _device(step):
    device = (PCI_DEVICES / step["bdf"]).resolve(strict=True)
    if _identity(device.stat()) != step["device_identity"]:
        raise ValueError(f"PCI device identity changed for {step['bdf']}")
    return device


@contextmanager
def _control(step, write=False):
    path = _device(step) / step["control"]
    flags = os.O_WRONLY if write else os.O_RDONLY
    fd = os.open(path, flags | os.O_NOFOLLOW)
    try:
        if _identity(os.fstat(fd)) != step["control_identity"]:
            raise ValueError(f"HCU control identity changed for {path}")
        yield fd
    finally:
        os.close(fd)


def _read_control(step):
    with _control(step) as fd:
        value = os.read(fd, 4096).decode("ascii").strip()
    return int(value) if step["profile"] in POWER_PROFILES else value


def _clock_state(device):
    table = (device / "pp_dpm_sclk").read_text(encoding="ascii")
    levels = []
    active_khz = None
    for line in table.splitlines():
        entry = CLOCK_LEVEL.fullmatch(line)
        if entry:
            frequency_khz = int(entry[2]) * 1000
            levels.append([int(entry[1]), frequency_khz])
            if entry[3]:
                active_khz = frequency_khz
    try:
        trace = int((device / "pp_sclk_trace").read_text(encoding="ascii"))
        if trace > 0:
            active_khz = trace
    except (FileNotFoundError, ValueError):
        pass
    return {"dpm_levels": levels, "sclk_khz": active_khz}


def _sample(step):
    current = _read_control(step)
    target = {"hcu": step["hcu"], "bdf": step["bdf"]}
    if step["profile"] in POWER_PROFILES:
        return {**target, "power_cap_uw": current}
    return {**target, "mode": current, **_clock_state(_device(step))}


def _delegate(profile):
    if profile in OPERATION_PROFILES:
        from . import hcu_operation

        return hcu_operation
    from . import hcu_load

    return hcu_load


def prepare(profile, target):
    number = target["hcu"]
    bdf = next((item["bdf"] for item in devices() if item["hcu"] == number), None)
    if bdf is None:
        raise ValueError(f"HCU {number} was not reported by hy-smi --showbus")
    device = (PCI_DEVICES / bdf).resolve(strict=True)
    step = {"profile": profile, "resource": f"pci:{bdf}", "hcu": number, "bdf": bdf,
            "device_identity": _identity(device.stat()), "seconds": target["seconds"]}
    if profile in CLOCK_MODES:
        control = device / "power_dpm_force_performance_level"
    elif profile in POWER_PROFILES:
        controls = list((device / "hwmon").glob("hwmon*/power1_cap"))
        if len(controls) != 1:
            raise ValueError(f"expected one power cap control for HCU {number} ({bdf})")
        control = controls[0]
    else:
        backend = _delegate(profile)
        if profile in OPERATION_PROFILES:
            backend.prepare(step, target)
        else:
            backend.prepare(step)
        return step
    info = control.lstat()
    if (not stat.S_ISREG(info.st_mode) or not info.st_mode & 0o222
            or os.statvfs(control).f_flag & os.ST_RDONLY or not os.access(control, os.W_OK)):
        raise ValueError(f"HCU control is not writable: {control}")
    step.update(control=str(control.relative_to(device)), control_identity=_identity(info))
    baseline = _sample(step)
    if profile in CLOCK_MODES:
        original = baseline["mode"]
        if original not in RESTORABLE_MODES:
            raise ValueError(f"cannot safely restore original HCU clock mode {original!r}; "
                             "the driver does not expose the original manual clock mask")
        if len({khz for _, khz in baseline["dpm_levels"]}) < 2 or baseline["sclk_khz"] is None:
            raise ValueError("cannot read multiple HCU clock levels and the current core clock")
        step.update(original=original, injected=CLOCK_MODES[profile], dpm_levels=baseline["dpm_levels"])
    else:
        minimum = max(1, int(control.with_name("power1_cap_min").read_text(encoding="ascii")))
        maximum = int(control.with_name("power1_cap_max").read_text(encoding="ascii"))
        original = baseline["power_cap_uw"]
        watts = target.get("value")
        if watts is None:
            minimum_watts = (minimum + MICROWATTS_PER_WATT - 1) // MICROWATTS_PER_WATT
            watts = max(minimum_watts, original // (2 * MICROWATTS_PER_WATT))
        injected = watts * MICROWATTS_PER_WATT
        if not minimum <= injected < original <= maximum:
            raise ValueError(f"power limit must be at least {minimum / MICROWATTS_PER_WATT:g} W "
                             f"and below the original {original / MICROWATTS_PER_WATT:g} W")
        step.update(original=original, injected=injected)
    if step["original"] == step["injected"]:
        raise ValueError(f"HCU {number} already has the requested setting {step['injected']!r}")
    if profile in PULSE_PROFILES:
        step.update(phase_on=True, pulse_transitions=0)
    return step


def observe(step):
    if "control" not in step:
        _device(step)
        return {**_delegate(step["profile"]).observe(step), "hcu": step["hcu"], "bdf": step["bdf"]}
    result = _sample(step)
    if step["profile"] in PULSE_PROFILES:
        result["pulse_transitions"] = step["pulse_transitions"]
    return result


def _change(step, expected, desired):
    if _read_control(step) != expected:
        raise ValueError("HCU control changed since the session took its snapshot")
    if "dpm_levels" in step and _clock_state(_device(step))["dpm_levels"] != step["dpm_levels"]:
        raise ValueError("HCU DPM table changed since the session took its snapshot")
    if expected != desired:
        data = f"{desired}\n".encode("ascii")
        with _control(step, write=True) as fd:
            if os.write(fd, data) != len(data):
                raise OSError("incomplete HCU control write")
    if _read_control(step) != desired:
        raise ValueError(f"driver did not read back the requested HCU setting {desired!r}")


def apply(step):
    if "control" not in step:
        _device(step)
        return {**_delegate(step["profile"]).apply(step), "hcu": step["hcu"], "bdf": step["bdf"]}
    _change(step, step["original"], step["injected"])
    return observe(step)


def restore(step):
    if "control" not in step:
        return {**_delegate(step["profile"]).restore(step), "hcu": step["hcu"], "bdf": step["bdf"]}
    current = _read_control(step)
    if current not in {step["original"], step["injected"]}:
        raise ValueError("HCU control changed outside the session; refusing to overwrite it")
    _change(step, current, step["original"])
    return observe(step)


def tick(step, elapsed):
    if step["profile"] not in PULSE_PROFILES:
        return False
    phase_on = int(elapsed // PULSE_SECONDS) % 2 == 0
    if phase_on == step["phase_on"]:
        return False
    original = step["original"]
    injected = step["injected"]
    _change(step, original if phase_on else injected, injected if phase_on else original)
    step["phase_on"] = phase_on
    step["pulse_transitions"] += 1
    return True
