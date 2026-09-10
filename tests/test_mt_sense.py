import json
import unittest
from unittest import mock

import config as config_mod
import sense as sense_mod


class FakeIface:
    def __init__(self, raise_on_send=None):
        self.sent = []
        self.raise_on_send = raise_on_send

    def sendData(self, data, **kwargs):
        if self.raise_on_send:
            raise self.raise_on_send
        self.sent.append((data, kwargs))
        return {"id": 1}


class MtSenseTests(unittest.TestCase):
    """MT Sense must not report 'no reply' as an error, must not stall on the
    library's blocking wait, and must only reconnect a genuinely dead radio."""

    def setUp(self):
        self.events = []

    def _run(self, iface, *, alive, window=0):
        with mock.patch.object(sense_mod, "SENSE_COLLECTION_WINDOW", window), \
             mock.patch.object(sense_mod, "push_to_sse",
                               side_effect=lambda payload: self.events.append(json.loads(payload))), \
             mock.patch("mesh._is_iface_alive", return_value=alive), \
             mock.patch("mesh._reconnect_disconnected") as reconnect, \
             mock.patch("mesh.send_position_request",
                        side_effect=(iface.raise_on_send if iface.raise_on_send else None)) as sender:
            sense_mod._sense_state["active"] = True
            sense_mod._sense_state["responses"] = []
            blocked = False
            try:
                sense_mod._run_sense_broadcast(iface, 0)
            except Exception:
                blocked = True
            return reconnect, sender, blocked

    def test_send_position_request_does_not_block_or_use_cli_handler(self):
        """The request must go out via sendData (no waitForPosition, no CLI handler)."""
        from mesh import send_position_request
        from meshtastic.protobuf import portnums_pb2
        iface = FakeIface()
        send_position_request(iface)
        self.assertEqual(len(iface.sent), 1)
        _data, kwargs = iface.sent[0]
        self.assertEqual(kwargs["portNum"], portnums_pb2.PortNum.POSITION_APP)
        self.assertTrue(kwargs["wantResponse"])
        self.assertTrue(callable(kwargs["onResponse"]))
        self.assertIsNone(kwargs["onResponse"]({"anything": 1}))  # no-op, never exits

    def test_library_timeout_is_not_an_error(self):
        exc = Exception("Timed out waiting for position")
        iface = FakeIface(raise_on_send=exc)
        reconnect, _sender, _blocked = self._run(iface, alive=True)
        reconnect.assert_not_called()
        errors = [e for e in self.events if e.get("error")]
        self.assertEqual(errors, [], f"timeout must not surface as an error: {self.events}")
        self.assertTrue(any(e.get("type") == "sense_done" for e in self.events))

    def test_dead_interface_still_reports_and_reconnects(self):
        exc = Exception("device reports readiness to read but returned no data")
        iface = FakeIface(raise_on_send=exc)
        reconnect, _sender, _blocked = self._run(iface, alive=False)
        reconnect.assert_called_once()
        errors = [e for e in self.events if e.get("error")]
        self.assertEqual(len(errors), 1, self.events)

    def test_silent_mode_still_blocks_the_broadcast(self):
        import mesh  # noqa: F401
        config_mod.CONFIG["silent_mode"] = True
        try:
            iface = FakeIface()
            _r, _sender, _blocked = self._run(iface, alive=True)
        finally:
            config_mod.CONFIG["silent_mode"] = False
        self.assertTrue(any("Silent Running" in (e.get("error") or "") for e in self.events), self.events)


if __name__ == "__main__":
    unittest.main()
