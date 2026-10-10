#!/usr/bin/env python3
"""Read whatever the kernel knows about this AMD GPU, and say what it does not.

This is the spike instrument for `workspace-szw.10`: the monitor has no GPU or
host signal at all, and before choosing an exporter somebody has to answer, on
*this* box (gfx1151, shared memory, distrobox + Vulkan, no ROCm), which of the
amdgpu signals are actually readable. Two of the answers were not obvious from
any exporter's README:

* the VRAM files exist but are close to useless on an APU whose BIOS carve-out
  was minimised — the number that moves with the model is **GTT**, because
  `amdgpu.gttsize` / `ttm.pages_limit` size the GTT domain and that is where the
  buffers land (kernel: `amdgpu_gtt_mgr.c`, `amdgpu_ttm.c`:
  `gtt_size = ttm_tt_pages_limit() << PAGE_SHIFT`);
* an unreadable attribute is **not** zero. amdgpu answers `gpu_busy_percent`
  and friends with `-EPERM` when the card is runtime-suspended
  (`amdgpu_pm_get_access_if_active()` refuses rather than waking the device).
  A collector that turns that into `0` invents an idle GPU. This script reports
  such an attribute as *absent* and records the reason instead.

Signal sources, all readable without ROCm and without root:

| what | where |
| --- | --- |
| VRAM / visible VRAM / **GTT** used+total | `/sys/class/drm/card*/device/mem_info_*_{total,used}` |
| GPU / memory busy % | `.../device/{gpu,mem}_busy_percent` |
| clocks (sclk/mclk/fclk/socclk/…) | `.../device/pp_dpm_*` (current state marked `*`) |
| temps, power, fans | `.../device/hwmon/hwmon*/{temp,power,fan}*` |
| runtime PM state | `.../device/power/runtime_status` |
| **per-process** GPU memory + engine time | `/proc/<pid>/fdinfo/*` (`drm-resident-memory-*`, `drm-engine-*`) |
| the tunables that define the ceilings | `/sys/module/{amdgpu,ttm}/parameters/*` (amdgpu's are 0600 → root) |

Modes::

    ./amdgpu_sysfs_probe.py probe                 # capability matrix, for pasting
    ./amdgpu_sysfs_probe.py metrics               # Prometheus text on stdout
    ./amdgpu_sysfs_probe.py textfile DIR          # write DIR/amdgpu.prom atomically
    ./amdgpu_sysfs_probe.py serve --port 9810     # scrape endpoint (loopback)
    ./amdgpu_sysfs_probe.py --sysfs-root /host/sys --proc-root /host/proc probe

`--sysfs-root` (and `--proc-root` for the fdinfo half) is what makes this
testable off the box: point them at the fixture (`fixtures/sysfs-gfx1151`) or at a
bind-mounted host `/sys`.

Stdlib only, like the rest of this repo. Nothing here is deployed by the spike;
run `probe` on the box and paste the output into the findings note.
"""

from __future__ import annotations

import argparse
import errno
import os
import re
import sys
import tempfile
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

# ---------------------------------------------------------------------------
# reading sysfs without lying
# ---------------------------------------------------------------------------

OK = "ok"                 # the only status that yields a value
MISSING = "missing"       # ENOENT / ENOTDIR / ENODEV — the kernel has no such file
DENIED = "denied"         # EACCES / EPERM — perms, or the driver refusing access
BUSY = "busy"             # EBUSY / EAGAIN — answering would wake the card
ERROR = "error"           # anything else, including an unparseable payload

_STATUS_CODES = {
    errno.ENOENT: MISSING, errno.ENOTDIR: MISSING, errno.ENODEV: MISSING,
    errno.ENXIO: MISSING, errno.EACCES: DENIED, errno.EPERM: DENIED,
    errno.EBUSY: BUSY, errno.EAGAIN: BUSY,
}


@dataclass
class Reading:
    """One sysfs attribute: a value, or an honest reason for not having one."""
    name: str
    status: str = MISSING
    value: float | None = None
    raw: str = ""
    detail: str = ""

    @property
    def usable(self) -> bool:
        return self.status == OK and self.value is not None

    @property
    def status_code(self) -> int:
        return 1 if self.status == OK else 0


def classify_error(err: OSError) -> str:
    """Bucket an errno into a fixed set of statuses (a label must not grow errnos)."""
    if err.errno is None:
        return ERROR
    return _STATUS_CODES.get(err.errno, ERROR)


def errno_name(err: OSError) -> str:
    code = getattr(errno, "errorcode", {})
    return code.get(err.errno, "").lower() or str(err.strerror or err)


def read_attr(path: Path, name: str, cast=float) -> Reading:
    """Read one attribute once. Never retry: retrying is how you hold a GPU awake."""
    try:
        text = path.read_text().strip()
    except OSError as err:
        return Reading(name, classify_error(err), detail=errno_name(err))
    except UnicodeDecodeError:
        return Reading(name, ERROR, detail="not text")
    if not text:
        return Reading(name, ERROR, detail="empty")
    try:
        return Reading(name, OK, float(cast(text)), raw=text)
    except (ValueError, TypeError):
        return Reading(name, ERROR, detail=f"unparsable {text[:32]!r}")


def leading_number(text: str) -> float:
    """`123`, `123456789`, `500.0 MHz` -> float of the leading number."""
    return float(text.split()[0])


#: mem_info_* attribute -> pool label
MEM_BYTES = {
    "mem_info_vram_total": ("vram", "total"),
    "mem_info_vram_used": ("vram", "used"),
    "mem_info_vis_vram_total": ("vis_vram", "total"),
    "mem_info_vis_vram_used": ("vis_vram", "used"),
    "mem_info_gtt_total": ("gtt", "total"),
    "mem_info_gtt_used": ("gtt", "used"),
}
BUSY_PCT = {"gpu_busy_percent": "gpu", "mem_busy_percent": "mem", "vcn_busy_percent": "vcn"}
DPM_CLOCKS = ("pp_dpm_sclk", "pp_dpm_mclk", "pp_dpm_fclk", "pp_dpm_socclk",
              "pp_dpm_vclk", "pp_dpm_dclk", "pp_dpm_dcefclk", "pp_dpm_pcie")
#: attributes whose payload is text: availability matters, the number does not
STATUS_ONLY = ("power_dpm_force_performance_level", "pcie_bw", "pp_cur_state", "unique_id")

_SIZE_UNITS = {"": 1, "b": 1, "kib": 1024, "mib": 1024 ** 2, "gib": 1024 ** 3,
               "kb": 1000, "mb": 1000 ** 2, "gb": 1000 ** 3}


def parse_size(text: str) -> float:
    """`18432 KiB`, `512 MiB`, `1024` -> bytes. fdinfo picks the unit itself."""
    parts = text.replace("\t", " ").split()
    n = float(parts[0])
    unit = parts[1].lower() if len(parts) > 1 else ""
    if unit not in _SIZE_UNITS:
        raise ValueError(f"unknown unit {unit!r} in {text!r}")
    return n * _SIZE_UNITS[unit]


# ---------------------------------------------------------------------------
# discovery
# ---------------------------------------------------------------------------

@dataclass
class Sensor:
    """One hwmon sensor that answered."""
    kind: str            # temp | power | fan
    key: str             # e.g. temp1
    chip: str            # hwmon device name, e.g. amdgpu
    reading: Reading


@dataclass
class Gpu:
    """One `/sys/class/drm/cardN` that the amdgpu driver claims."""
    card: str
    device: Path
    uevent: dict[str, str] = field(default_factory=dict)
    attrs: dict[str, Reading] = field(default_factory=dict)
    clocks: list[Reading] = field(default_factory=list)
    sensors: list[Sensor] = field(default_factory=list)

    @property
    def labels(self) -> str:
        return (f'card="{self.card}",pci="{self.uevent.get("PCI_SLOT_NAME", "unknown")}"'
                f',chip="{self.uevent.get("PCI_ID", "unknown")}"')

    def every_reading(self) -> list[Reading]:
        return [*self.attrs.values(), *self.clocks, *(s.reading for s in self.sensors)]


def _read_keyval(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    try:
        for line in path.read_text().splitlines():
            if "=" in line:
                k, _, v = line.partition("=")
                out[k.strip()] = v.strip()
    except OSError:
        pass
    return out


def discover(sysfs_root: Path) -> tuple[list[Gpu], list[str]]:
    """Find the cards amdgpu claims. Returns (cards, notes) explaining exclusions.

    `cardN/device` is a **symlink** into `/sys/devices/pci.../`, and hwmon lives
    behind another one. A container that bind-mounted only `/sys/class/drm` sees
    dangling links and gets nothing; this reports that as a note instead of
    emitting a card full of zeroes. That is the container-mount answer of this
    spike, and it is checkable: see `make_fixture.py --broken-symlink`.
    """
    cards: list[Gpu] = []
    notes: list[str] = []
    drm_root = sysfs_root / "class" / "drm"
    if not drm_root.is_dir():
        return cards, [f"{drm_root}: not a directory — is /sys mounted, is a GPU present?"]
    for entry in sorted(drm_root.iterdir()):
        if not re.fullmatch(r"card[0-9]+", entry.name):
            continue                                  # card0-DP-1 connector dirs
        link = entry / "device"
        try:
            resolved = link.resolve(strict=True)
        except OSError:
            notes.append(f"{entry.name}: device symlink does not resolve — /sys is "
                         f"mounted too narrowly, mount it whole (read-only)")
            continue
        uevent = _read_keyval(resolved / "uevent")
        if uevent.get("DRIVER") != "amdgpu":
            notes.append(f"{entry.name}: driver={uevent.get('DRIVER', '<none>')} — not amdgpu, skipped")
            continue
        cards.append(Gpu(card=entry.name, device=resolved, uevent=uevent))
    if not cards and not notes:
        notes.append("no card* directories under class/drm")
    return cards, notes


def collect(gpu: Gpu) -> Gpu:
    """Read every attribute of one card, exactly once each."""
    dev = gpu.device
    for attr in MEM_BYTES:
        gpu.attrs[attr] = read_attr(dev / attr, attr, leading_number)
    for attr in BUSY_PCT:
        gpu.attrs[attr] = read_attr(dev / attr, attr, leading_number)
    gpu.attrs["power/runtime_status"] = read_attr(
        dev / "power" / "runtime_status", "power/runtime_status",
        lambda s: 1.0 if s.strip() == "suspended" else 0.0)
    for attr in STATUS_ONLY:
        gpu.attrs[attr] = read_attr(dev / attr, attr, lambda s: 1.0)

    for dpm in DPM_CLOCKS:
        r = read_attr(dev / dpm, dpm, cast=lambda s: 1.0)     # a table, not a number
        if r.status == OK:
            # payload is "0: 800MHz\n1: 1200MHz *\n"; the `*` line is the current state
            lines = [ln for ln in r.raw.splitlines() if ln.strip()]
            current = next((ln for ln in lines if "*" in ln), lines[0] if lines else "")
            m = re.search(r"(\d+(?:\.\d+)?)\s*MHz", current, re.IGNORECASE)
            if m:
                gpu.clocks.append(Reading(dpm, OK, float(m.group(1)) * 1_000_000,
                                          raw=current.strip(),
                                          detail=f"{len(lines)} states"))
            else:
                gpu.clocks.append(Reading(dpm, ERROR, detail=f"no MHz in {current[:24]!r}"))
        else:
            gpu.clocks.append(r)

    hwmon_root = dev / "hwmon"
    sensors = sorted(hwmon_root.glob("hwmon*")) if hwmon_root.is_dir() else []
    if not sensors:
        gpu.attrs["hwmon"] = Reading("hwmon", MISSING, detail="no hwmon child directory")
    for mon in sensors:
        try:
            chip = (mon / "name").read_text().strip()
        except OSError:
            chip = mon.name
        for entry in sorted(mon.iterdir()):
            m = re.fullmatch(r"(temp|power|fan|in|energy)(\d+)(?:_(input|average|max|label))?",
                             entry.name)
            if not m or m.group(3) == "label":
                continue
            kind, idx, what = m.group(1), m.group(2), m.group(3) or "input"
            gpu.sensors.append(Sensor(kind, f"{kind}{idx}", chip,
                                      read_attr(entry, f"{kind}{idx}_{what}", leading_number)))
    return gpu


# ---------------------------------------------------------------------------
# per-process GPU memory — the missing half of the KV-cache correlation
# ---------------------------------------------------------------------------

FD_KEYS = {  # fdinfo key -> pool label (amdgpu_fdinfo.c / drm_print_memory_stats)
    "drm-resident-memory-vram": "vram",
    "drm-resident-memory-gtt": "gtt",
    "drm-resident-memory-cpu": "cpu",
    "drm-purgable-memory-vram": "vram_purgable",
    "drm-purgable-memory-gtt": "gtt_purgable",
    "drm-total-memory-vram": "vram_total",
    "drm-total-memory-gtt": "gtt_total",
}


def process_gpu_memory(proc_root: Path = Path("/proc")) -> list[tuple[str, int, str, float]]:
    """(comm, pid, pool, bytes) for every process holding DRM buffer objects.

    Free, kernel-provided and ROCm-free: amdgpu accounts memory per open DRM fd,
    so this answers *which process grew the GTT* — the half that
    `llm:kv_cache_usage_ratio` cannot. `/proc/<pid>/fdinfo` is only readable for
    your own processes, so this is per-uid unless the collector runs as root.
    `--proc-root` exists so the parsing is testable (and so a container can point
    it at a mounted host `/proc`).
    """
    out: list[tuple[str, int, str, float]] = []
    if not proc_root.is_dir():
        return out
    for pid_dir in sorted(proc_root.iterdir()):
        if not pid_dir.name.isdigit():
            continue
        try:
            comm = (pid_dir / "comm").read_text().strip()
        except OSError:
            continue
        fdinfo = pid_dir / "fdinfo"
        if not fdinfo.is_dir():
            continue
        try:
            entries = list(os.scandir(fdinfo))
        except OSError:
            continue
        for fd in entries:
            try:
                text = Path(fd.path).read_text()
            except OSError:
                continue                      # vanished between scandir and read, or not ours
            if "drm-" not in text:
                continue
            totals: dict[str, float] = {}
            for line in text.splitlines():
                key, _, value = line.partition(":")
                if key not in FD_KEYS:
                    continue
                try:
                    totals[key] = parse_size(value.strip())
                except ValueError:
                    continue
            for key, nbytes in totals.items():
                out.append((comm, int(pid_dir.name), FD_KEYS[key], nbytes))
    return out


# ---------------------------------------------------------------------------
# exposition
# ---------------------------------------------------------------------------

_METRIC_HELP = {
    "amdgpu_present": "1 per amdgpu card the probe could open.",
    "amdgpu_memory_used_bytes": "Memory pool in use (pool=vram|vis_vram|gtt).",
    "amdgpu_memory_total_bytes": "Memory pool size as sized by the driver/tunables.",
    "amdgpu_busy_percent": "SMU-reported busy percentage (kind=gpu|mem|vcn).",
    "amdgpu_clock_ticks_per_second": "Current DPM clock in Hz (domain=sclk|mclk|...).",
    "amdgpu_temperature_celsius": "hwmon temperature sensor.",
    "amdgpu_power_watts": "hwmon power sensor.",
    "amdgpu_fan_rpm": "hwmon fan tachometer.",
    "amdgpu_runtime_suspended": "1 when the card is in runtime suspend.",
    "amdgpu_attr_status": "Attribute availability: 1 for the status that holds "
                          "(status=ok|missing|denied|busy|error). Absence of a value is a "
                          "fact; a zero for a refused read is a lie.",
    "amdgpu_process_memory_used_bytes": "Per-process GPU memory from /proc/<pid>/fdinfo.",
    "amdgpu_probe_notes": "1 per probe-level note (a skipped card, a missing /sys).",
}


def _series(name: str, labels: str, value: float) -> str:
    return f"{name}{{{labels}}} {value:g}"


def render_metrics(cards: list[Gpu], notes: list[str], with_fdinfo: bool,
                   proc_root: Path = Path("/proc")) -> str:
    """Prometheus text form. Absent values are absent, never zero-filled."""
    lines: list[str] = []
    for name, help_text in _METRIC_HELP.items():
        if name == "amdgpu_process_memory_used_bytes" and not with_fdinfo:
            continue
        lines += [f"# HELP {name} {help_text}", f"# TYPE {name} gauge"]

    for gpu in cards:
        labels = gpu.labels
        lines.append(_series("amdgpu_present", f'{labels},driver="{gpu.uevent.get("DRIVER", "")}"', 1))
        for attr, reading in gpu.attrs.items():
            if attr in MEM_BYTES:
                pool, which = MEM_BYTES[attr]
                metric = ("amdgpu_memory_total_bytes" if which == "total"
                          else "amdgpu_memory_used_bytes")
                if reading.usable:
                    lines.append(_series(metric, f'{labels},pool="{pool}"', reading.value))
            elif attr in BUSY_PCT and reading.usable:
                lines.append(_series("amdgpu_busy_percent",
                                     f'{labels},kind="{BUSY_PCT[attr]}"', reading.value))
            elif attr == "power/runtime_status" and reading.usable:
                lines.append(_series("amdgpu_runtime_suspended", labels, reading.value))
            lines.append(_series("amdgpu_attr_status",
                                 f'{labels},attr="{attr}",status="{reading.status}"',
                                 reading.status_code))
        for clock in gpu.clocks:
            if clock.usable:
                lines.append(_series("amdgpu_clock_ticks_per_second",
                                     f'{labels},domain="{clock.name.removeprefix("pp_dpm_")}"',
                                     clock.value))
            lines.append(_series("amdgpu_attr_status",
                                 f'{labels},attr="{clock.name}",status="{clock.status}"',
                                 clock.status_code))
        for sensor in gpu.sensors:
            metric = {"temp": "amdgpu_temperature_celsius", "power": "amdgpu_power_watts",
                      "fan": "amdgpu_fan_rpm"}.get(sensor.kind)
            if metric and sensor.reading.usable:
                # hwmon units are per-type: millidegrees, microwatts, RPM
                value = sensor.reading.value
                if sensor.kind == "temp":
                    value /= 1000
                elif sensor.kind == "power":
                    value /= 1_000_000
                lines.append(_series(metric,
                                     f'{labels},sensor="{sensor.key}",hwmon="{sensor.chip}"', value))
            lines.append(_series("amdgpu_attr_status",
                                 f'{labels},attr="{sensor.reading.name}",'
                                 f'status="{sensor.reading.status}"',
                                 sensor.reading.status_code))

    if with_fdinfo:
        for comm, pid, pool, nbytes in process_gpu_memory(proc_root):
            lines.append(_series("amdgpu_process_memory_used_bytes",
                                 f'comm="{comm}",pid="{pid}",pool="{pool}"', nbytes))
    for note in notes:
        lines.append(_series("amdgpu_probe_notes", f'note="{note.replace(chr(34), "")}"', 1))
    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# the capability matrix — what running this on the box is *for*
# ---------------------------------------------------------------------------

def render_probe(cards: list[Gpu], notes: list[str], with_fdinfo: bool,
                 sysfs_root: Path, proc_root: Path = Path("/proc")) -> str:
    out = [f"# amdgpu sysfs probe — {time.strftime('%Y-%m-%d %H:%M:%S')}",
           f"kernel={os.uname().release}  sysfs_root={sysfs_root}  uid={os.getuid()}", ""]
    if notes:
        out += ["## notes", *[f"- {n}" for n in notes], ""]
    for gpu in cards:
        out.append(f"## {gpu.card}  driver={gpu.uevent.get('DRIVER')}  "
                   f"pci={gpu.uevent.get('PCI_SLOT_NAME')}  id={gpu.uevent.get('PCI_ID')}")
        width = max((len(r.name) for r in gpu.every_reading()), default=10)
        out.append(f"{'attribute'.ljust(width)}  {'status':8}  value")
        for group in (list(gpu.attrs.values()), gpu.clocks, [s.reading for s in gpu.sensors]):
            for r in group:
                shown = r.raw if len(r.raw) <= 40 else r.raw[:37] + "..."
                if not shown and r.value is not None:
                    shown = f"{r.value:g}"
                if r.status != OK and r.detail:
                    shown = f"{shown} ({r.detail})"
                out.append(f"{r.name.ljust(width)}  {r.status:8}  {shown}")
            out.append("")
    if with_fdinfo:
        rows = process_gpu_memory(proc_root)
        out.append("## per-process GPU memory (/proc/*/fdinfo)")
        if not rows:
            out.append("(nothing readable — a DRM client must be owned by this uid)")
        by_proc: dict[tuple[str, int], dict[str, float]] = {}
        for comm, pid, pool, nbytes in rows:
            by_proc.setdefault((comm, pid), {})
            by_proc[(comm, pid)][pool] = by_proc[(comm, pid)].get(pool, 0.0) + nbytes
        for (comm, pid), pools in sorted(by_proc.items()):
            pretty = "  ".join(f"{k}={v / 2**20:.1f}MiB" for k, v in sorted(pools.items()))
            out.append(f"{comm}(pid {pid}): {pretty}")
        out.append("")
    out.append("Legend: ok=value read · missing=no such attribute on this kernel/driver "
               "· denied=EPERM/EACCES (perms, or the driver refusing a suspended card) "
               "· busy=read would wake the card · error=payload not as expected")
    return "\n".join(out)


# ---------------------------------------------------------------------------
# sinks
# ---------------------------------------------------------------------------

def write_textfile(directory: Path, text: str, name: str = "amdgpu.prom") -> Path:
    """Atomic write: node_exporter's textfile collector must never see half a file."""
    directory.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", dir=directory, prefix=".amdgpu.", delete=False) as fh:
        fh.write(text)
        tmp = Path(fh.name)
    os.chmod(tmp, 0o644)
    target = directory / name
    os.replace(tmp, target)
    return target


class _Handler(BaseHTTPRequestHandler):
    source: tuple[Path, bool, Path] = (Path("/sys"), False, Path("/proc"))
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):        # quiet — a scraped exporter is chatty otherwise
        pass

    def do_GET(self):                    # noqa: N802 - BaseHTTPRequestHandler API
        if self.path.split("?")[0] not in ("/metrics", "/"):
            self.send_error(404)
            return
        root, fdinfo, proc_root = self.source
        cards, notes = discover(root)
        body = render_metrics([collect(g) for g in cards], notes, fdinfo, proc_root).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; version=0.0.4")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def serve(sysfs_root: Path, port: int, host: str, fdinfo: bool, proc_root: Path) -> None:
    _Handler.source = (sysfs_root, fdinfo, proc_root)
    httpd = ThreadingHTTPServer((host, port), _Handler)
    print(f"listening on http://{host}:{port}/metrics (sysfs={sysfs_root})", file=sys.stderr)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("mode", choices=["probe", "metrics", "textfile", "serve"], nargs="?", default="probe")
    ap.add_argument("target", nargs="?", help="textfile directory (textfile mode)")
    ap.add_argument("--sysfs-root", default="/sys")
    ap.add_argument("--proc-root", default="/proc", help="for /proc/<pid>/fdinfo (tests, containers)")
    ap.add_argument("--port", type=int, default=9810)
    ap.add_argument("--host", default="127.0.0.1", help="loopback default, deliberately")
    ap.add_argument("--no-fdinfo", action="store_true",
                    help="skip /proc/<pid>/fdinfo per-process accounting")
    args = ap.parse_args(argv)
    root = Path(args.sysfs_root)
    proc_root = Path(args.proc_root)
    fdinfo = not args.no_fdinfo

    cards, notes = discover(root)
    collected = [collect(g) for g in cards]
    if args.mode == "metrics":
        sys.stdout.write(render_metrics(collected, notes, fdinfo, proc_root))
    elif args.mode == "probe":
        sys.stdout.write(render_probe(collected, notes, fdinfo, root, proc_root))
    elif args.mode == "textfile":
        if not args.target:
            ap.error("textfile mode needs a directory")
        print(write_textfile(Path(args.target),
                             render_metrics(collected, notes, fdinfo, proc_root)))
    elif args.mode == "serve":
        serve(root, args.port, args.host, fdinfo, proc_root)
    return 0 if cards else 2


if __name__ == "__main__":
    raise SystemExit(main())
