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
        fake_iface.nodesByNum = {}
        self.iface = fake_iface
        from meshtastic.protobuf import channel_pb2
        ln.channels = [channel_pb2.Channel() for _ in range(3)]
        for i, c in enumerate(ln.channels):
            c.role = 2
            c.settings.name = ["LongFast", "Slovenija", "Test"][i]
            c.settings.uplink_enabled = False
            c.settings.downlink_enabled = False
        self.written_channels = []
        ln.writeChannel.side_effect = lambda idx: self.written_channels.append(idx)
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

    # ── a hidden device-write failure must not look like success (field bug 2026-09-12)
    def test_position_send_failure_is_surfaced(self):
        """The admin send used to be swallowed ('best-effort'): the API said OK, the
        radio kept its old position, and the optimistic local update hid it in the UI."""
        self.ln._sendAdmin.side_effect = RuntimeError("serial link is busy")
        r = self._post("/config/position", {"fixed_position": True, "lat": 46.040314,
                                            "lon": 14.504438, "alt": 242})
        self.assertEqual(r.status_code, 500, r.get_data(as_text=True))
        self.assertIn("Could not send the position", r.get_json().get("error", ""))

    def test_position_send_failure_does_not_fake_a_local_position(self):
        """And it must not write the position into the iface's own copy either."""
        self.ln._sendAdmin.side_effect = RuntimeError("serial link is busy")
        r = self._post("/config/position", {"fixed_position": True, "lat": 46.040314,
                                            "lon": 14.504438, "alt": 242})
        self.assertEqual(r.status_code, 500)
        self.assertEqual(self.iface.nodesByNum, {}, "no optimistic position on failure")

    def test_position_save_success_still_returns_ok(self):
        r = self._post("/config/position", {"fixed_position": True, "lat": 46.040314,
                                            "lon": 14.504438, "alt": 242})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertEqual(self.ln._sendAdmin.call_count, 1)

    def test_config_read_exposes_the_map_report_settings(self):
        self.ln.moduleConfig.mqtt.map_report_settings.should_report_location = True
        r = self.client.get(f"/api/radio/{RADIO}/config")
        body = r.get_json()
        for key in ("mqtt_map_location", "mqtt_map_interval", "mqtt_map_precision"):
            self.assertIn(key, body, f"{key} must be exposed (the map opt-in gates map reports)")
        self.assertTrue(body["mqtt_map_location"])

    # ── the two controls OM was missing (Filip: "add the toggles") ──────────────
    def test_map_report_optin_is_written(self):
        """Without should_report_location the firmware skips the map report entirely."""
        r = self._post("/config/mqtt", {"mqtt_enabled": True, "mqtt_map": True,
                                        "mqtt_map_location": True,
                                        "mqtt_map_interval": 3600,
                                        "mqtt_map_precision": 13})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        mrs = self.ln.moduleConfig.mqtt.map_report_settings
        self.assertTrue(mrs.should_report_location)
        self.assertEqual(mrs.publish_interval_secs, 3600)
        self.assertEqual(mrs.position_precision, 13)

    def test_map_report_precision_out_of_range_is_rejected(self):
        r = self._post("/config/mqtt", {"mqtt_map_location": True, "mqtt_map_precision": 20})
        self.assertEqual(r.status_code, 400, r.get_data(as_text=True))

    def test_map_interval_blank_leaves_the_device_value_alone(self):
        self.ln.moduleConfig.mqtt.map_report_settings.publish_interval_secs = 3600
        r = self._post("/config/mqtt", {"mqtt_map_location": True, "mqtt_map_interval": 0})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.ln.moduleConfig.mqtt.map_report_settings.publish_interval_secs, 3600)

    # ── /config/reload: the device-truth read for verify-after-write ──
    def test_config_reload_asks_for_a_module_section(self):
        r = self._post("/config/reload", {"section": "mqtt"})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertEqual(r.get_json().get("section"), "mqtt")
        self.ln.requestConfig.assert_called_once()
        field = self.ln.requestConfig.call_args[0][0]
        self.assertEqual(field.name, "mqtt")
        self.assertEqual(field.containing_type.name, "LocalModuleConfig",
                         "mqtt lives in LocalModuleConfig, not LocalConfig")

    def test_config_reload_asks_for_a_local_section(self):
        r = self._post("/config/reload", {"section": "lora"})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        field = self.ln.requestConfig.call_args[0][0]
        self.assertEqual(field.name, "lora")
        self.assertEqual(field.containing_type.name, "LocalConfig")

    def test_config_reload_rejects_an_unknown_section(self):
        r = self._post("/config/reload", {"section": "definitely_not_a_section"})
        self.assertEqual(r.status_code, 400)
        self.ln.requestConfig.assert_not_called()

    def test_config_reload_returns_503_when_the_radio_is_not_connected(self):
        with mock.patch("routes.radio.get_iface_by_radio", return_value=None):
            r = self.client.post(f"/api/radio/{RADIO}/config/reload", json={"section": "mqtt"})
        self.assertEqual(r.status_code, 503)
        self.ln.requestConfig.assert_not_called()

    # ── `bool("false") == True` trap (sweep 2026-09-12) ──
    def test_string_false_is_not_treated_as_true(self):
        """A client sending the STRING "false" must not switch the flag on."""
        r = self._post("/config/lora", {"region": 3, "ok_to_mqtt": "false", "ignore_mqtt": "false"})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertFalse(self.ln.localConfig.lora.config_ok_to_mqtt, 'the string "false" must mean False')
        self.assertFalse(self.ln.localConfig.lora.ignore_mqtt)

    def test_string_true_is_treated_as_true(self):
        r = self._post("/config/lora", {"region": 3, "ok_to_mqtt": "true"})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(self.ln.localConfig.lora.config_ok_to_mqtt)

    # ── LoRa-level MQTT participation: OK to MQTT / ignore MQTT (absent from OM) ──
    def test_lora_mqtt_participation_flags_are_written(self):
        """config_ok_to_mqtt sets the ok_to_mqtt bit on our packets (what community maps
        ask for); ignore_mqtt refuses MQTT-relayed packets. Both are LoRa config fields."""
        r = self._post("/config/lora", {"region": 3, "modem_preset": 0, "tx_power": 27,
                                        "hop_limit": 6, "ok_to_mqtt": True,
                                        "ignore_mqtt": False})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertTrue(self.ln.localConfig.lora.config_ok_to_mqtt)
        self.assertFalse(self.ln.localConfig.lora.ignore_mqtt)

    def test_lora_save_without_the_new_flags_keeps_them(self):
        """An older cached page must not silently clear them."""
        self.ln.localConfig.lora.config_ok_to_mqtt = True
        r = self._post("/config/lora", {"region": 3, "modem_preset": 0, "tx_power": 27, "hop_limit": 6})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(self.ln.localConfig.lora.config_ok_to_mqtt)

    def test_config_read_exposes_the_lora_mqtt_flags(self):
        self.ln.localConfig.lora.config_ok_to_mqtt = True
        self.ln.localConfig.lora.ignore_mqtt = True
        r = self.client.get(f"/api/radio/{RADIO}/config")
        body = r.get_json()
        self.assertIn("lora_ok_to_mqtt", body)
        self.assertIn("lora_ignore_mqtt", body)
        self.assertTrue(body["lora_ok_to_mqtt"])
        self.assertTrue(body["lora_ignore_mqtt"])

    # ── the map-report interval minimum is 3600 s (documented) ──
    def test_map_interval_below_the_firmware_minimum_is_rejected(self):
        r = self._post("/config/mqtt", {"mqtt_map_location": True, "mqtt_map_interval": 900})
        self.assertEqual(r.status_code, 400, r.get_data(as_text=True))
        self.assertIn("3600", r.get_json().get("error", ""))

    def test_map_interval_at_the_minimum_is_accepted(self):
        r = self._post("/config/mqtt", {"mqtt_map_location": True, "mqtt_map_interval": 3600})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertEqual(self.ln.moduleConfig.mqtt.map_report_settings.publish_interval_secs, 3600)

    def test_channel_uplink_and_downlink_are_written(self):
        r = self._post("/channels/0", {"role": 2, "name": "LongFast", "psk_type": "keep",
                                       "uplink_enabled": True, "downlink_enabled": False})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertTrue(self.ln.channels[0].settings.uplink_enabled)
        self.assertFalse(self.ln.channels[0].settings.downlink_enabled)
        self.assertEqual(self.written_channels, [0])

    def test_channel_save_without_the_flags_keeps_them(self):
        """A save that omits the keys (e.g. an older page) must not silently clear them."""
        self.ln.channels[1].settings.uplink_enabled = True
        r = self._post("/channels/1", {"role": 2, "name": "Slovenija", "psk_type": "keep"})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(self.ln.channels[1].settings.uplink_enabled)

    def test_channel_list_exposes_the_flags(self):
        self.ln.channels[0].settings.uplink_enabled = True
        r = self.client.get(f"/api/radio/{RADIO}/channels")
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        body = r.get_json()
        self.assertTrue(body[0]["uplink_enabled"])
        self.assertFalse(body[0]["downlink_enabled"])

    def test_telemetry_save_uses_writeConfig(self):
        r = self._post("/config/telemetry", {"tel_device_update": 1800})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertEqual(self.written, ["telemetry"])


if __name__ == "__main__":
    unittest.main()
