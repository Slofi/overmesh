import unittest
from unittest import mock

import mesh_mc


class _LineSerial:
    """Records line writes; property style (as connection_made uses)."""

    def __init__(self):
        self.writes = []
        self._dtr = None
        self._rts = None

    @property
    def dtr(self):
        return self._dtr

    @dtr.setter
    def dtr(self, v):
        self._dtr = v
        self.writes.append(("dtr", v))

    @property
    def rts(self):
        return self._rts

    @rts.setter
    def rts(self, v):
        self._rts = v
        self.writes.append(("rts", v))

    def fileno(self):
        raise OSError("no fd in test")


class _PulseSerial:
    """Records setDTR/setRTS calls; method style (as _dtr_reset_port uses)."""

    def __init__(self):
        self.writes = []

    def setDTR(self, v):
        self.writes.append(("dtr", v))

    def setRTS(self, v):
        self.writes.append(("rts", v))

    def close(self):
        pass


class _FakeTransport:
    def __init__(self, serial_obj):
        self.serial = serial_obj


class _FakeCx:
    def __init__(self, port):
        self._om_port = port
        self.transport = None

        class _Ev:
            def set(self):
                pass
        self._connected_event = _Ev()


class McSerialLineHandlingTests(unittest.TestCase):
    """Line handling must match the transport type. Getting this wrong is silent:
    the radio enumerates and simply never answers appstart.

    CD's ProMicro (nRF52840, ttyACM) went offline when the CP2102 fix (release
    RTS then DTR) reached it — TinyUSB CDC gates its TX on DTR."""

    def test_port_classification(self):
        self.assertTrue(mesh_mc._mc_port_is_usb_cdc("/dev/ttyACM0"))
        self.assertFalse(mesh_mc._mc_port_is_usb_cdc("/dev/ttyUSB1"))
        self.assertFalse(mesh_mc._mc_port_is_usb_cdc(""))
        self.assertFalse(mesh_mc._mc_port_is_usb_cdc("/dev/ttyACM0".replace("ACM", "USB")))

    def _connection_made(self, port):
        proto = object.__new__(mesh_mc.OMSerialConnection.MCSerialClientProtocol)
        proto.cx = _FakeCx(port)
        ser = _LineSerial()
        proto.connection_made(_FakeTransport(ser))
        return ser

    def test_cdc_device_keeps_dtr_asserted(self):
        ser = self._connection_made("/dev/ttyACM0")
        self.assertTrue(ser.dtr, "USB-CDC needs DTR asserted or the firmware stays mute")
        self.assertFalse(ser.rts)

    def test_uart_bridge_releases_rts_then_dtr(self):
        ser = self._connection_made("/dev/ttyUSB1")
        self.assertFalse(ser.dtr)
        self.assertFalse(ser.rts)
        self.assertEqual(ser.writes, [("rts", False), ("dtr", False)])

    def test_reset_pulse_skipped_for_cdc(self):
        with mock.patch("serial.Serial") as fake_serial:
            sent = mesh_mc._dtr_reset_port("/dev/ttyACM0", "MC")
        fake_serial.assert_not_called()
        self.assertFalse(sent)

    def test_reset_pulse_sent_for_uart_bridge(self):
        ser = _PulseSerial()
        with mock.patch("serial.Serial", return_value=ser), mock.patch("time.sleep"):
            sent = mesh_mc._dtr_reset_port("/dev/ttyUSB1", "ERA-3")
        self.assertTrue(sent)
        self.assertEqual(ser.writes, [("dtr", True), ("rts", False), ("dtr", False)])


if __name__ == "__main__":
    unittest.main()
