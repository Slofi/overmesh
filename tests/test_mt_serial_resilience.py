import errno
import io
import unittest
from unittest import mock

import serial


class MtSerialResilienceTests(unittest.TestCase):
    """The meshtastic reader treats any read exception as fatal. Our
    OMSerialInterface must retry the benign CP210x spurious error and still
    surface real errors (armed so the failing cases are reachable)."""

    def _mk(self):
        import mesh
        obj = object.__new__(mesh.OMSerialInterface)
        obj._spurious_reads = 0
        return obj

    def _raise(self, exc):
        def _r(self, length):
            raise exc
        return _r

    def test_benign_eagain_is_retried(self):
        obj = self._mk()
        exc = serial.SerialException("device reports readiness to read but returned no data")
        exc.errno = errno.EAGAIN
        with mock.patch("meshtastic.stream_interface.StreamInterface._readBytes", self._raise(exc)):
            self.assertEqual(obj._readBytes(1), b"")
        self.assertEqual(obj._spurious_reads, 1)

    def test_benign_message_without_errno_is_retried(self):
        obj = self._mk()
        exc = serial.SerialException("device reports readiness to read but returned no data (device disconnected or multiple access on port?)")
        with mock.patch("meshtastic.stream_interface.StreamInterface._readBytes", self._raise(exc)):
            self.assertEqual(obj._readBytes(1), b"")

    def test_real_unplug_error_is_reraised(self):
        obj = self._mk()
        exc = serial.SerialException("Input/output error")
        exc.errno = errno.EIO
        with mock.patch("meshtastic.stream_interface.StreamInterface._readBytes", self._raise(exc)):
            with self.assertRaises(serial.SerialException):
                obj._readBytes(1)

    def test_data_passes_through_and_resets_counter(self):
        obj = self._mk()
        obj._spurious_reads = 7
        with mock.patch("meshtastic.stream_interface.StreamInterface._readBytes", lambda self, n: b"\x01\x02"):
            self.assertEqual(obj._readBytes(2), b"\x01\x02")
        self.assertEqual(obj._spurious_reads, 0)

    def test_endless_benign_errors_eventually_raise(self):
        obj = self._mk()
        obj._spurious_reads = obj._MAX_SPURIOUS_READS
        exc = serial.SerialException("device reports readiness to read but returned no data")
        exc.errno = errno.EAGAIN
        with mock.patch("meshtastic.stream_interface.StreamInterface._readBytes", self._raise(exc)):
            with self.assertRaises(serial.SerialException):
                obj._readBytes(1)


if __name__ == "__main__":
    unittest.main()
