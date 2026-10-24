"""OPS-TOC as the OM Position source never falls back to a serial port.

History: S454 found the proxy logging "falling back to /dev/ttyACM0" forever while nothing fell back (that path
was the MC radio's own port). S471 made the fallback real and then hit the worst case: OPS-TOC down, the saved
fallback path /dev/ttyACM0 now held a freshly flashed Meshtastic board, and OM opened it. Filip then decided:
"If OPS-TOC doesn't have a fix, OM doesn't have a fix ... this time I want no automation."

Contract: with OPS-TOC chosen, an unreachable OPS-TOC means no position. The proxy reader never opens or
nominates a serial port, and a sustained outage warns at most once per _PROXY_WARN_EVERY.
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


class GpsProxyNeverFallsBackTests(unittest.TestCase):
    def test_unreachable_ops_toc_opens_nothing_and_warns_once(self):
        stop = threading.Event()
        with mock.patch.object(gps.urllib.request, "urlopen", side_effect=OSError("connection refused")), \
             mock.patch.object(gps, "_PROXY_RETRY_SECONDS", 0.01), \
             mock.patch.object(gps, "_gps_start") as start, \
             mock.patch.object(gps, "gps_device_check") as check, \
             mock.patch.object(gps.log, "warning") as warn:
            t = threading.Thread(target=gps._gps_proxy_reader, args=("http://localhost:8090", stop), daemon=True)
            t.start()
            deadline = time.time() + 1.0
            while time.time() < deadline:
                if warn.call_count:
                    time.sleep(0.15)  # room for several more failed cycles
                    break
                time.sleep(0.01)
            stop.set()
            t.join(timeout=2)

        start.assert_not_called()
        check.assert_not_called()   # it does not even look for a serial device
        self.assertEqual(warn.call_count, 1, f"a sustained outage must warn once, got {warn.call_args_list}")
        msg = warn.call_args[0][0]
        self.assertIn("no position until it answers", msg)
        self.assertNotIn("using", msg)

    def test_the_proxy_reader_has_no_serial_port_parameter(self):
        import inspect
        self.assertEqual(list(inspect.signature(gps._gps_proxy_reader).parameters), ["proxy_url", "stop_event"])
        self.assertFalse(hasattr(gps, "_gps_proxy_fallback_port"), "the fallback hand-over must be gone")


if __name__ == "__main__":
    unittest.main()
