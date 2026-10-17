"""The GPS proxy must not claim a fallback it never performs.

Found live on CD 2026-10-17 (S454): with OPS-TOC stopped, OM logged
"GPS: OPS-TOC unreachable after 5 tries - falling back to /dev/ttyACM0" every 10 seconds, forever. The watchdog
restarted the proxy each time, so nothing fell back, and /dev/ttyACM0 is the MC radio's own port, not a GPS.

New contract:
  * a fallback is used only when the port is present AND not one of our radios (gps_port_conflict);
  * otherwise the proxy keeps retrying and warns at most once per _PROXY_WARN_EVERY, never "falling back";
  * the usable-fallback case is actually wired through the watchdog (the old code always restarted the proxy).
"""
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

_TEST_DIR = tempfile.TemporaryDirectory(prefix="overmesh-test-")
_TEST_ROOT = Path(_TEST_DIR.name)
_CONFIG_PATH = _TEST_ROOT / "config.json"
_DATA_DIR = _TEST_ROOT / "data"
_DATA_DIR.mkdir(parents=True, exist_ok=True)
_CONFIG_PATH.write_text(json.dumps({"nodes": [], "mc_nodes": [], "gps": {}, "silent_mode": False}), encoding="utf-8")

os.environ.setdefault("OVERMESH_CONFIG", str(_CONFIG_PATH))
os.environ.setdefault("OVERMESH_DATA_DIR", str(_DATA_DIR))
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

import gps  # noqa: E402


class GpsProxyFallbackTests(unittest.TestCase):
    def setUp(self):
        gps._gps_proxy_fallback_port = ""

    def test_a_radio_port_is_never_used_as_a_gps_fallback(self):
        stop = threading.Event()
        with mock.patch.object(gps.urllib.request, "urlopen", side_effect=OSError("connection refused")), \
             mock.patch.object(gps, "_PROXY_RETRY_SECONDS", 0.01), \
             mock.patch.object(gps, "gps_port_present", return_value=True), \
             mock.patch.object(gps, "gps_port_conflict", return_value={"network": "MC", "name": "MC"}), \
             mock.patch.object(gps.log, "warning") as warn:
            t = threading.Thread(target=gps._gps_proxy_reader,
                                 args=("http://localhost:8090", "/dev/ttyACM0", stop), daemon=True)
            t.start()
            # 5 fails to reach the limit, then more cycles: enough to prove the message is throttled.
            deadline = time.time() + 1.0
            while time.time() < deadline:
                if warn.call_count:
                    time.sleep(0.15)  # room for several more failed cycles
                    break
                time.sleep(0.01)
            stop.set()
            t.join(timeout=2)

        self.assertEqual(gps._gps_proxy_fallback_port, "",
                         "a port that is the MC radio must not be advertised as a fallback")
        self.assertEqual(warn.call_count, 1, f"a sustained outage must warn once, got {warn.call_count}: {warn.call_args_list}")
        msg = warn.call_args[0][0]
        self.assertIn("still retrying", msg)
        self.assertIn("MC radio", msg)
        self.assertNotIn("falling back", msg)

    def test_a_usable_serial_port_is_handed_to_the_watchdog(self):
        with mock.patch.object(gps.urllib.request, "urlopen", side_effect=OSError("connection refused")), \
             mock.patch.object(gps, "_PROXY_RETRY_SECONDS", 0.01), \
             mock.patch.object(gps, "gps_port_present", return_value=True), \
             mock.patch.object(gps, "gps_port_conflict", return_value=None), \
             mock.patch.object(gps.log, "warning") as warn:
            gps._gps_proxy_reader("http://localhost:8090", "/dev/ttyUSB0", threading.Event())

        self.assertEqual(gps._gps_proxy_fallback_port, "/dev/ttyUSB0",
                         "a present, non-radio port must be offered to the watchdog")
        self.assertTrue(warn.called)
        self.assertIn("using the configured serial port", warn.call_args[0][0])

    def test_watchdog_honours_the_fallback_request(self):
        src = Path(REPO, "gps.py").read_text(encoding="utf-8")
        self.assertIn("if _gps_proxy_fallback_port:", src,
                      "the watchdog must check the fallback request before restarting the proxy")
        self.assertIn("port = _gps_proxy_fallback_port", src,
                      "the watchdog must read the requested port, not the configured one")


if __name__ == "__main__":
    unittest.main()
