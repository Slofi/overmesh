import asyncio
import unittest
from unittest import mock

import config as config_mod
import mesh_mc


class _FakeCommands:
    def __init__(self, recorder):
        self.recorder = recorder

    async def set_coords(self, lat, lon):
        self.recorder.append(("set_coords", lat, lon))
        return mock.Mock(type=mesh_mc.EventType.OK)

    async def send_advert(self, *a, **k):
        self.recorder.append(("send_advert", a, k))
        return mock.Mock(type=mesh_mc.EventType.OK)


class _FakeMc:
    def __init__(self, recorder):
        self.commands = _FakeCommands(recorder)


class SilentRunningTxGateTests(unittest.TestCase):
    """Silent Running must gate transmissions, not config writes.

    History: position updates used to re-advertise, and those raw
    mc.commands.send_advert() calls never consulted the flag (fixed 2026-09-10).
    Then on 2026-10-16 (Filip, S453) OM became manual-only for adverts and that
    re-advert was removed outright, so the assertion is now the stronger one:
    a position update writes to the device and transmits nothing, in either mode.
    """

    def setUp(self):
        self.recorder = []
        self._silent = config_mod.CONFIG.get("silent_mode", False)
        self.addCleanup(lambda: config_mod.CONFIG.__setitem__("silent_mode", self._silent))

    def _run_coords(self):
        fake = _FakeMc(self.recorder)
        with mock.patch.object(mesh_mc, "_get_mc", return_value=(fake, None)), \
             mock.patch.object(mesh_mc, "push_to_sse"):
            asyncio.run(mesh_mc._set_coords_async("mc1", 46.0, 14.0))

    def test_soft_gate_flags(self):
        config_mod.CONFIG["silent_mode"] = True
        self.assertFalse(mesh_mc._mc_tx_allowed_soft("test"))
        config_mod.CONFIG["silent_mode"] = False
        self.assertTrue(mesh_mc._mc_tx_allowed_soft("test"))

    def test_position_update_writes_but_transmits_nothing_while_silent(self):
        config_mod.CONFIG["silent_mode"] = True
        self._run_coords()
        kinds = [r[0] for r in self.recorder]
        self.assertIn("set_coords", kinds, "the local device write must still happen")
        self.assertNotIn("send_advert", kinds, "Silent Running must suppress any advert")

    def test_position_update_never_advertises_when_not_silent(self):
        """Adverts are manual-only (Filip, 2026-10-16) — a coords write must not transmit.

        This test used to assert the opposite (``["set_coords", "send_advert"]``). The
        re-advert was removed on his order, so it now guards the new policy: the only
        ways an advert leaves OM are the Advert button, Scan, and a managed repeater's
        own Local/Flood advert.
        """
        config_mod.CONFIG["silent_mode"] = False
        self._run_coords()
        kinds = [r[0] for r in self.recorder]
        self.assertEqual(kinds, ["set_coords"],
                         "adverts are manual-only: pressing Advert is the only way")


if __name__ == "__main__":
    unittest.main()
