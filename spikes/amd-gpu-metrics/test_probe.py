"""Tests for the spike's sysfs reader — standard library only, like the code.

    python3 -m unittest discover -s spikes/amd-gpu-metrics -p "test_*.py"
    python3 spikes/amd-gpu-metrics/test_probe.py -v

Everything runs against the generated fixture (`make_fixture.py`), so the whole
suite is runnable in a container with no GPU — which is where it was written.
"""

from __future__ import annotations

import os
import re
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import amdgpu_sysfs_probe as probe            # noqa: E402
import make_fixture                           # noqa: E402

FIXTURES = Path(__file__).parent / "fixtures"
SERIES = re.compile(r'^[a-z_]+(\{[^}]*\})? [-+0-9.eE]+(\s+\d+)?$')
PROM = re.compile(r"^[a-z_:][a-zA-Z0-9_:]*$")


def fixture(root_name: str) -> Path:
    """Build a fixture in a temp dir (mode-000 files need a fresh tree each time)."""
    tmp = Path(tempfile.mkdtemp(prefix="probe-test-"))
    src = {"gfx1151": {}, "broken": {"broken_symlink": True}, "denied": {"denied": True}}[root_name]
    card = make_fixture.build(tmp / "sys", **src)
    return card.parents[2]


def collect_one(root: Path) -> probe.Gpu:
    cards, _ = probe.discover(root)
    assert len(cards) == 1, f"expected exactly one card, got {cards}"
    return probe.collect(cards[0])


class Discover(unittest.TestCase):
    def test_finds_the_amdgpu_card_through_the_device_symlink(self):
        root = fixture("gfx1151")
        cards, notes = probe.discover(root)
        self.assertEqual(notes, [], "no card should be skipped in the good fixture")
        self.assertEqual(len(cards), 1)
        gpu = probe.collect(cards[0])
        self.assertEqual(gpu.card, "card0")
        self.assertEqual(gpu.uevent["DRIVER"], "amdgpu")
        self.assertTrue(gpu.device.is_symlink() or (root / "class/drm/card0/device").is_symlink())

    def test_narrow_mount_is_reported_not_silently_zeroed(self):
        """/sys/class/drm without /sys/devices: the device symlink dangles.

        This is the container-mount answer in code form: a reader gets a note,
        not a card whose every value is zero.
        """
        root = fixture("broken")
        cards, notes = probe.discover(root)
        self.assertEqual(cards, [])
        self.assertTrue(any("symlink" in n and "mount" in n for n in notes), notes)

    def test_missing_sysfs_is_a_note_too(self):
        cards, notes = probe.discover(Path("/nonexistent-sys"))
        self.assertEqual(cards, [])
        self.assertTrue(notes)


class Values(unittest.TestCase):
    def setUp(self):
        self.gpu = collect_one(fixture("gfx1151"))

    def test_gtt_is_the_pool_that_holds_the_model(self):
        gtt_total = self.gpu.attrs["mem_info_gtt_total"].value
        gtt_used = self.gpu.attrs["mem_info_gtt_used"].value
        self.assertEqual(gtt_total, 125829120000)     # amdgpu.gttsize=120000 MiB
        self.assertEqual(gtt_used, 36936790016)
        # the APU's VRAM is the minimised BIOS carve-out: two orders of magnitude
        # smaller than the GTT, i.e. not the signal to alert on here
        self.assertLess(self.gpu.attrs["mem_info_vram_total"].value, gtt_total / 100)

    def test_busy_percent_and_runtime_status(self):
        self.assertEqual(self.gpu.attrs["gpu_busy_percent"].value, 43)
        self.assertEqual(self.gpu.attrs["power/runtime_status"].status, probe.OK)
        self.assertEqual(self.gpu.attrs["power/runtime_status"].value, 0)  # "active"

    def test_absent_attribute_is_absent_not_zero(self):
        """`vcn_busy_percent` is not in the fixture, mimicking an APU."""
        r = self.gpu.attrs["vcn_busy_percent"]
        self.assertEqual(r.status, probe.MISSING)
        self.assertIsNone(r.value)
        text = probe.render_metrics([self.gpu], [], with_fdinfo=False)
        self.assertNotIn('kind="vcn"', text, "an absent attribute must not appear as 0")
        self.assertIn('attr="vcn_busy_percent",status="missing"', text)

    def test_dpm_clock_reads_the_starred_state(self):
        clocks = {c.name: c.value for c in self.gpu.clocks}
        self.assertEqual(clocks["pp_dpm_sclk"], 1250 * 10**6)   # "4: 1250MHz *"
        self.assertEqual(clocks["pp_dpm_mclk"], 1600 * 10**6)
        self.assertNotIn("pp_dpm_pcie", {c.name for c in self.gpu.clocks if c.usable},
                         "pp_dpm_pcie is absent from the fixture and must stay absent")

    def test_hwmon_sensors_are_scaled(self):
        kinds = {(s.kind, s.key): s.reading.value for s in self.gpu.sensors}
        self.assertEqual(kinds[("temp", "temp1")], 55111)        # millidegrees, raw
        self.assertEqual(kinds[("power", "power1")], 78125000)   # microwatts
        text = probe.render_metrics([self.gpu], [], with_fdinfo=False)
        self.assertIn("amdgpu_temperature_celsius{", text)
        self.assertRegex(text, r"amdgpu_temperature_celsius\{[^}]*sensor=\"temp1\"[^}]*\} 55\.111")
        self.assertRegex(text, r"amdgpu_power_watts\{[^}]*\} 78\.125")

    @unittest.skipIf(os.geteuid() == 0, "root can read a 000-mode file")
    def test_unreadable_attribute_is_denied_not_zero(self):
        gpu = collect_one(fixture("denied"))
        r = gpu.attrs["mem_info_gtt_used"]
        self.assertTrue(r.status.startswith(probe.DENIED), r.status)
        text = probe.render_metrics([gpu], [], with_fdinfo=False)
        self.assertNotIn('pool="gtt"} 3', text)


class Exposition(unittest.TestCase):
    def setUp(self):
        self.gpu = collect_one(fixture("gfx1151"))
        self.text = probe.render_metrics([self.gpu], ["a note"], with_fdinfo=False)

    def test_every_line_is_valid_prometheus_text(self):
        for line in self.text.splitlines():
            if line.startswith("#"):
                continue
            self.assertRegex(line, SERIES, f"bad series line: {line!r}")

    def test_every_metric_has_exactly_one_help_and_type(self):
        names = {n for n in (l.split()[0] for l in self.text.splitlines()
                             if l and not l.startswith("#"))
                 for n in [n.split("{")[0]]}
        for name in names:
            self.assertRegex(name, PROM, name)
            self.assertEqual(self.text.count(f"# TYPE {name} "), 1, f"{name}: TYPE must appear once")
            self.assertEqual(self.text.count(f"# HELP {name} "), 1, f"{name}: HELP must appear once")

    def test_labels_are_stable_enough_to_graph(self):
        text = self.text
        self.assertIn('card="card0"', text)
        self.assertIn('pci="0000:c3:00.0"', text)
        self.assertIn('pool="gtt"', text)
        self.assertIn('domain="sclk"', text)

    def test_notes_become_a_series_not_a_stderr_line(self):
        self.assertIn('amdgpu_probe_notes{note="a note"} 1', self.text)

    def test_textfile_write_lands_one_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = probe.write_textfile(Path(tmp), self.text)
            self.assertEqual(path.name, "amdgpu.prom")
            self.assertEqual(path.read_text(), self.text)
            self.assertEqual([p.name for p in Path(tmp).iterdir()], ["amdgpu.prom"],
                             "the temp file must be renamed, not left behind")


class ProcessAccounting(unittest.TestCase):
    def test_fdinfo_keys_are_parsed_with_their_units(self):
        root = Path(tempfile.mkdtemp(prefix="proc-")) / "proc"
        fd = root / "4242" / "fdinfo"
        fd.mkdir(parents=True)
        (root / "4242" / "comm").write_text("llama-server\n")
        (fd / "7").write_text(
            "pos:    0\nflags:        02142012\nmnt_id:       7\n"
            "drm-driver:\tamdgpu\ndrm-client-id:\t3\n"
            "drm-pdev:\t0000:c3:00.0\n"
            "drm-resident-memory-vram:\t512 MiB\n"
            "drm-resident-memory-gtt:\t18432 KiB\n"
            "drm-purgable-memory-gtt:\t0\n"
            "drm-engine-gfx:\t1234 ns\n"
        )
        (root / "77").mkdir()
        (root / "77" / "comm").write_text("bash\n")
        (root / "77" / "fdinfo").mkdir()
        (root / "77" / "fdinfo" / "1").write_text("pos:\t0\n")

        rows = probe.process_gpu_memory(root)
        pools = {pool: nbytes for comm, pid, pool, nbytes in rows if comm == "llama-server"}
        self.assertEqual(pools["vram"], 512 * 1024 ** 2)         # "512 MiB"
        self.assertEqual(pools["gtt"], 18432 * 1024)             # "18432 KiB"
        self.assertEqual(pools["gtt_purgable"], 0)
        self.assertNotIn("bash", [comm for comm, *_ in rows])

    def test_parse_size_rejects_nonsense(self):
        with self.assertRaises(ValueError):
            probe.parse_size("12 frobnigates")


if __name__ == "__main__":
    unittest.main(verbosity=2)
