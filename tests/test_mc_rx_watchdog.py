import unittest

import mesh_mc


class RxCountersFrozenTests(unittest.TestCase):
    """The startup RX watchdog used to reboot a healthy radio purely because the
    counters did not move — which a quiet mesh reproduces exactly (seen live:
    recv 44->44, queue=0, uptime 562s). The frozen test must stay tight, and the
    watchdog now escalates to an active probe before rebooting."""

    def _s(self, recv, rx_air):
        return {"recv": recv, "rx_air": rx_air}

    def test_identical_samples_are_frozen(self):
        self.assertTrue(mesh_mc._rx_counters_frozen(self._s(44, 39), self._s(44, 39)))

    def test_any_received_packet_is_not_frozen(self):
        self.assertFalse(mesh_mc._rx_counters_frozen(self._s(44, 39), self._s(45, 39)))

    def test_airtime_alone_is_enough(self):
        """rx_air can advance on traffic the packet counter does not surface."""
        self.assertFalse(mesh_mc._rx_counters_frozen(self._s(44, 39), self._s(44, 41)))

    def test_counter_going_backwards_counts_as_movement(self):
        """A radio that rebooted in between resets its counters — that is not a
        stuck receiver, so it must not be frozen (negative delta)."""
        self.assertFalse(mesh_mc._rx_counters_frozen(self._s(500, 400), self._s(3, 1)))


if __name__ == "__main__":
    unittest.main()
