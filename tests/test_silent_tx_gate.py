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

    Position updates kept transmitting a re-advert while silent — the raw
    mc.commands.send_advert() calls never consulted the flag.
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

    def test_position_update_skips_advert_but_still_writes_while_silent(self):
        config_mod.CONFIG["silent_mode"] = True
        self._run_coords()
        kinds = [r[0] for r in self.recorder]
        self.assertIn("set_coords", kinds, "the local device write must still happen")
        self.assertNotIn("send_advert", kinds, "Silent Running must suppress the re-advert")

    def test_position_update_advertises_when_not_silent(self):
        config_mod.CONFIG["silent_mode"] = False
        self._run_coords()
        kinds = [r[0] for r in self.recorder]
        self.assertEqual(kinds, ["set_coords", "send_advert"])


if __name__ == "__main__":
    unittest.main()
