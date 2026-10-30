"""S477: OM must read the GPS with ONE reader per port and only clean sentences.

Port of the OPS-TOC defect fixed the same day (2026-10-30). OM's gps.py had the
same two shapes:

  * `_gps_start()` set the stop flag, slept 0.2 s and started a new thread with
    NO join. A watchdog tick (every 5 s) racing a Settings save could therefore
    leave an orphan reader holding a second fd on the same /dev/ttyACM0. Two
    readers do not get a copy of the stream each: the kernel hands bytes to
    whoever asks first, so both assemble lines out of each other's halves.
  * `_parse_gpgga()` trusts field positions and ignored the NMEA checksum, so a
    torn line became a "valid" fix hundreds of metres away (and `sats` 2
    against a real 12) for a second or two.

`test_concurrent_starts_leave_one_reader` fails on the pre-fix gps.py, so this
suite can actually fail.
"""
import json
import os
import sys
import tempfile
import threading
import time
import types
import unittest
from pathlib import Path
from unittest import mock

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _REPO)

_TEST_DIR = tempfile.TemporaryDirectory(prefix="overmesh-gps-reader-test-")
_TEST_ROOT = Path(_TEST_DIR.name)
_CONFIG_PATH = _TEST_ROOT / "config.json"
_DATA_DIR = _TEST_ROOT / "data"
_DATA_DIR.mkdir(parents=True, exist_ok=True)
_CONFIG_PATH.write_text(
    json.dumps({"nodes": [], "mc_nodes": [], "gps": {}, "silent_mode": False}),
    encoding="utf-8",
)
os.environ.setdefault("OVERMESH_CONFIG", str(_CONFIG_PATH))
os.environ.setdefault("OVERMESH_DATA_DIR", str(_DATA_DIR))

import gps as gps_mod  # noqa: E402

# Sentences from the NMEA standard's own examples: checksums are correct.
_GOOD = [
    "$GPGGA,123519,4807.038,N,01131.000,E,1,08,0.9,545.4,M,46.9,M,,*47",
    "$GPRMC,123519,A,4807.038,N,01131.000,E,022.4,084.4,230394,003.1,W*6A",
    "$GPGSV,2,1,08,01,40,083,46,02,17,308,41,12,07,344,39,14,22,228,45*75",
]


class FakeSerial:
    """Stands in for pyserial: counts open ports and replays scripted chunks."""

    instances = []
    script = []
    lock = threading.Lock()

    def __init__(self):
        self.is_open = False
        self.timeout = None
        self.write_timeout = None
        self.port = None
        self.baudrate = None
        self.dtr = None

    def open(self):
        self.is_open = True
        with FakeSerial.lock:
            FakeSerial.instances.append(self)

    def close(self):
        self.is_open = False

    def reset_input_buffer(self):
        pass

    def write(self, _data):
        return len(_data)

    @property
    def in_waiting(self):
        return 0

    def read(self, _size=1):
        if FakeSerial.script:
            return FakeSerial.script.pop(0)
        time.sleep(0.02)
        return b""

    def readline(self):
        if FakeSerial.script:
            chunk = FakeSerial.script.pop(0)
            if b"\n" in chunk:
                head, _, tail = chunk.partition(b"\n")
                if tail:
                    FakeSerial.script.insert(0, tail)
                return head + b"\n"
            return chunk
        time.sleep(0.02)
        return b""

    @classmethod
    def open_count(cls):
        with cls.lock:
            return sum(1 for s in cls.instances if s.is_open)


def _live_readers():
    return sum(1 for t in threading.enumerate() if t.name.endswith("(_gps_reader)") and t.is_alive())


class GpsReaderTests(unittest.TestCase):
    def setUp(self):
        self._backup_state = dict(gps_mod.gps_state)
        self._backup_runtime = dict(gps_mod._gps_runtime)
        FakeSerial.instances.clear()
        FakeSerial.script.clear()
        fake_serial = types.ModuleType("serial")
        fake_serial.Serial = FakeSerial
        self._serial_patch = mock.patch.dict(sys.modules, {"serial": fake_serial})
        self._serial_patch.start()
        for name, value in (
            ("gps_port_present", lambda port: True),
            ("_baud_for_port", lambda port: 9600),
            ("_gps_init", lambda ser: None),
        ):
            p = mock.patch.object(gps_mod, name, value)
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(self._serial_patch.stop)
        self.addCleanup(self._restore)
        self.addCleanup(mock.patch.stopall)

    def _restore(self):
        try:
            gps_mod._gps_stop()
        except Exception:
            pass
        FakeSerial.script.clear()
        FakeSerial.instances.clear()
        gps_mod.gps_state.clear()
        gps_mod.gps_state.update(self._backup_state)
        gps_mod._gps_runtime.clear()
        gps_mod._gps_runtime.update(self._backup_runtime)

    # ── checksum ─────────────────────────────────────────────────────────────
    def test_checksum_accepts_published_sentences(self):
        for line in _GOOD:
            self.assertTrue(gps_mod._nmea_ok(line), f"rejected a valid sentence: {line}")

    def test_checksum_rejects_torn_sentences(self):
        line = _GOOD[0]
        bad = [
            line[:20] + line[40:],            # bytes stolen in the middle
            line.replace(",1,08,", ",08,"),   # dropped field, checksum not updated
            line[:-2],                        # checksum cut off
            line.replace("*47", ""),          # no checksum
            line.replace("*47", "*48"),       # wrong checksum
            line[1:],                         # missing '$'
            "",
        ]
        for item in bad:
            self.assertFalse(gps_mod._nmea_ok(item), f"accepted a torn sentence: {item!r}")

    # ── one reader per port ──────────────────────────────────────────────────
    def test_concurrent_starts_leave_one_reader(self):
        def start():
            gps_mod._gps_start("/dev/ttyFAKE0")

        threads = [threading.Thread(target=start) for _ in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=20)
        time.sleep(0.4)
        self.assertEqual(FakeSerial.open_count(), 1, f"{FakeSerial.open_count()} ports left open")
        self.assertEqual(_live_readers(), 1, f"{_live_readers()} live _gps_reader threads")

    def test_stop_releases_the_port(self):
        gps_mod._gps_start("/dev/ttyFAKE0")
        time.sleep(0.3)
        gps_mod._gps_stop()
        self.assertEqual(FakeSerial.open_count(), 0, "a reader survived _gps_stop()")

    # ── framing ──────────────────────────────────────────────────────────────
    def test_line_split_across_usb_reads_is_stitched(self):
        gps_mod.gps_state.update({"lat": None, "lon": None, "fix": False})
        gps_mod._gps_runtime["bad_lines"] = 0
        line = b"$GPGGA,123519,4807.038,N,01131.000,E,1,08,0.9,545.4,M,46.9,M,,*47\r\n"
        FakeSerial.script[:] = [line[:15], line[15:40], line[40:]]
        gps_mod._gps_start("/dev/ttyFAKE0")
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and gps_mod.gps_state["lat"] is None:
            time.sleep(0.02)
        self.assertIsNotNone(gps_mod.gps_state["lat"], "the stitched sentence never parsed")
        self.assertAlmostEqual(gps_mod.gps_state["lat"], 48.11730, places=4)
        self.assertAlmostEqual(gps_mod.gps_state["lon"], 11.51667, places=4)
        self.assertEqual(gps_mod.gps_state["sats"], 8)
        self.assertEqual(int(gps_mod._gps_runtime.get("bad_lines") or 0), 0,
                         gps_mod._gps_runtime.get("bad_samples"))

    def test_garbled_line_is_counted_not_parsed(self):
        gps_mod.gps_state.update({"lat": None, "lon": None, "fix": False})
        gps_mod._gps_runtime["bad_lines"] = 0
        broken = b"$GPGGA,123519,4807.038,N,01131.000,E,08,0.9\r\n"
        FakeSerial.script[:] = [broken]
        gps_mod._gps_start("/dev/ttyFAKE0")
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and not gps_mod._gps_runtime.get("bad_lines"):
            time.sleep(0.02)
        self.assertIsNone(gps_mod.gps_state["lat"], "a garbled line became a position")
        self.assertEqual(int(gps_mod._gps_runtime.get("bad_lines") or 0), 1)
        self.assertEqual(gps_mod._gps_runtime.get("bad_samples"), ["$GPGGA,123519,4807.038,N,01131.000,E,08,0.9"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
