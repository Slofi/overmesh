"""MT radio config saves must use APIs that exist in the installed meshtastic lib.

Two live 500s on 2026-09-12 came from exactly this class of drift:
  * POST /api/radio/<id>/config/mqtt    -> Node.writeModuleConfig() does not exist (2.7.10)
  * POST /api/radio/<id>/config/network -> NetworkConfig has no wifi_ap_mode field
Both were swallowed by `except Exception: ... 500`, and the UI showed only "HTTP 500".

The handler tests run the REAL handlers with a fake iface whose localNode is a
Mock(spec=Node) — so a call to a non-existent method raises, and assigning to a
non-existent protobuf field raises — i.e. the tests are armed against this bug class.
They need the meshtastic package (skip where it is absent, e.g. the dev box; the
deploy hosts have it).
"""
import io
import os
import re
import sys
import unittest
from unittest import mock

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

try:
    import meshtastic  # noqa: F401
    from meshtastic.node import Node
    from meshtastic.protobuf import localonly_pb2 as L
    HAS_MT = True
except Exception:                                     # pragma: no cover
    HAS_MT = False

RADIO = "node_test1"


class SourceHygieneTests(unittest.TestCase):
    """These run everywhere (no meshtastic needed)."""

    def test_no_writeModuleConfig_call_anywhere(self):
        bad = []
        for path in ("routes/radio.py", "routes/nodes.py", "mesh.py", "helpers.py"):
            try:
                src = io.open(os.path.join(REPO, path), encoding="utf-8").read()
            except OSError:
                continue
            for i, line in enumerate(src.splitlines(), 1):
                if "writeModuleConfig" in line:
                    bad.append(f"{path}:{i}")
        self.assertEqual(bad, [], "Node.writeModuleConfig does not exist in meshtastic 2.7.10")


@unittest.skipUnless(HAS_MT, "meshtastic package not installed here")
class FieldAndMethodTests(unittest.TestCase):
    def test_every_config_field_om_assigns_exists(self):
        src = io.open(os.path.join(REPO, "routes/radio.py"), encoding="utf-8").read()
        local, module = L.LocalConfig(), L.LocalModuleConfig()
        alias_re = re.compile(r"(\w+)\s*=\s*[\w.\"']*?(local|module)Config(?:\.([a-z_]+))?\b")
        assign_re = re.compile(r"(\w+)\.([a-z_0-9]+)\s*=")

        aliases, bad = {}, []
        for lineno, line in enumerate(src.splitlines(), 1):
            for name, kind, section in alias_re.findall(line):
                holder = local if kind == "local" else module
                try:
                    aliases[name] = getattr(holder, section) if section else holder
                except AttributeError:
                    bad.append(f"{lineno}: section {section} does not exist")
            for name, field in assign_re.findall(line):
                msg = aliases.get(name)
                if msg is None:
                    continue
                if field not in [f.name for f in msg.DESCRIPTOR.fields]:
                    bad.append(f"routes/radio.py:{lineno}: {name}.{field} does not exist on "
                               f"{msg.DESCRIPTOR.name}")
        self.assertEqual(bad, [], "OM assigns config fields the installed lib does not have")

    def test_write_methods_used_exist(self):
        src = io.open(os.path.join(REPO, "routes/radio.py"), encoding="utf-8").read()
        for name in sorted(set(re.findall(r"localNode\.(write[A-Za-z]+)\(", src))):
            self.assertTrue(hasattr(Node, name), f"Node.{name} does not exist")


@unittest.skipUnless(HAS_MT, "meshtastic package not installed here")
class HandlerTests(unittest.TestCase):
    """Drive the real handlers through the Flask test client."""

    def setUp(self):
        import app as app_module
        self.app_module = app_module
        self.client = app_module.app.test_client()

        ln = mock.Mock(spec=Node)                      # non-existent methods raise
        ln.localConfig = L.LocalConfig()               # real protobuf: bad fields raise
        ln.moduleConfig = L.LocalModuleConfig()
        self.written = []
        ln.writeConfig.side_effect = lambda name: self.written.append(name)
        fake_iface = mock.Mock()
        fake_iface.localNode = ln
        fake_iface.myInfo = None
        fake_iface.nodes = {}
        self.ln = ln
        self.patch = mock.patch("routes.radio.get_iface_by_radio", return_value=fake_iface)
        self.patch.start()

    def tearDown(self):
        self.patch.stop()

    def _post(self, path, payload):
        return self.client.post(f"/api/radio/{RADIO}{path}", json=payload)

    def test_network_save_succeeds_and_ignores_unsupported_ap_mode(self):
        r = self._post("/config/network", {"wifi_enabled": True, "wifi_ssid": "MyNet",
                                           "wifi_psk": "secret", "wifi_ap_mode": True})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        body = r.get_json()
        self.assertTrue(body.get("ok"))
        self.assertEqual(body.get("ignored"), ["wifi_ap_mode"])
        self.assertEqual(self.ln.localConfig.network.wifi_ssid, "MyNet")
        self.assertEqual(self.written, ["network"])

    def test_mqtt_save_uses_writeConfig(self):
        r = self._post("/config/mqtt", {"mqtt_enabled": True, "mqtt_address": "mqtt.meshnet.si",
                                        "mqtt_username": "u", "mqtt_password": "p", "mqtt_tls": True,
                                        "mqtt_root": "si/meshnet/slovenia"})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertEqual(self.written, ["mqtt"])
        self.assertTrue(self.ln.moduleConfig.mqtt.enabled)
        self.assertEqual(self.ln.moduleConfig.mqtt.address, "mqtt.meshnet.si")
        self.assertEqual(self.ln.moduleConfig.mqtt.root, "si/meshnet/slovenia",
                         "the Root topic (community servers need their own) must be written")

    def test_config_read_exposes_the_root_topic(self):
        self.ln.moduleConfig.mqtt.root = "si/meshnet/slovenia"
        r = self.client.get(f"/api/radio/{RADIO}/config")
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertEqual(r.get_json().get("mqtt_root"), "si/meshnet/slovenia")

    def test_telemetry_save_uses_writeConfig(self):
        r = self._post("/config/telemetry", {"tel_device_update": 1800})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertEqual(self.written, ["telemetry"])


if __name__ == "__main__":
    unittest.main()
