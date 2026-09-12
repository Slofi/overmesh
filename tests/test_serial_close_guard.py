"""Sweep 2026-09-12: the library's reader thread must not traceback on a closed stream.

Field evidence: on MT reconnect the journal showed
    Exception in thread stream reader: ... OSError: [Errno 9] Bad file descriptor
from meshtastic/stream_interface.py `_disconnected()` -> `stream.close()`, because
OM's reconnect path had already closed it.
"""
import unittest
from unittest import mock

import mesh  # noqa: F401  (importing installs the guard)


class SerialCloseGuardTests(unittest.TestCase):
    def test_guard_is_installed(self):
        from meshtastic import stream_interface
        self.assertTrue(getattr(stream_interface.StreamInterface._disconnected, "_om_guarded", False),
                        "the guard must be installed at import time")

    def test_closing_an_already_closed_stream_is_swallowed(self):
        from meshtastic import stream_interface
        fake = mock.Mock()
        fake.stream.close.side_effect = OSError(9, "Bad file descriptor")
        stream_interface.StreamInterface._disconnected(fake)   # must not raise

    def test_other_errors_still_propagate(self):
        from meshtastic import stream_interface
        fake = mock.Mock()
        fake.stream.close.side_effect = RuntimeError("programming error, not a race")
        with self.assertRaises(RuntimeError):
            stream_interface.StreamInterface._disconnected(fake)


if __name__ == "__main__":
    unittest.main()
