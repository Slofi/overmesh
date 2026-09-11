"""MT message notifications must name the channel, like the MC side does.

Filip: MC notifications show "MC <channel name>" + message; MT showed
"<sender> on CH<index>". These tests cover the shared naming helper, the payload
field, and the client title wiring.
"""
import io
import os
import sys
import unittest

sys.path.insert(0, "/home/slofi/overmesh")

import helpers  # noqa: E402


class _Settings:
    def __init__(self, name):
        self.name = name


class _Channel:
    def __init__(self, index, name=None, has_settings=True):
        self.index = index
        self.settings = _Settings(name) if has_settings else None


class _LocalNode:
    def __init__(self, channels):
        self.channels = channels


class _Iface:
    def __init__(self, channels):
        self.localNode = _LocalNode(channels)


class MtChannelNameTests(unittest.TestCase):
    def test_named_channel(self):
        iface = _Iface([_Channel(0, "Primary"), _Channel(1, "Don't Panic")])
        self.assertEqual(helpers.mt_channel_name(iface, 1), "Don't Panic")

    def test_unnamed_index_zero_is_primary(self):
        iface = _Iface([_Channel(0, ""), _Channel(1, "Don't Panic")])
        self.assertEqual(helpers.mt_channel_name(iface, 0), "Primary")

    def test_unnamed_other_index_falls_back_to_ch_number(self):
        iface = _Iface([_Channel(2, None, has_settings=False)])
        self.assertEqual(helpers.mt_channel_name(iface, 2), "CH2")

    def test_index_absent_from_the_radio(self):
        iface = _Iface([_Channel(0, "Primary")])
        self.assertEqual(helpers.mt_channel_name(iface, 7), "CH7")

    def test_missing_index_none_treated_as_zero(self):
        iface = _Iface([_Channel(0, "Primary")])
        self.assertEqual(helpers.mt_channel_name(iface, None), "Primary")

    def test_no_localnode_or_channels_never_raises(self):
        class Bare:
            pass
        self.assertEqual(helpers.mt_channel_name(Bare(), 3), "CH3")
        self.assertEqual(helpers.mt_channel_name(_Iface(None), 0), "Primary")

    def test_broken_channel_object_never_raises(self):
        class Exploding:
            @property
            def channels(self):
                raise RuntimeError("boom")
        self.assertEqual(helpers.mt_channel_name(_Iface([]), 4), "CH4")
        self.assertEqual(helpers.mt_channel_name(_Iface([Exploding()]), 4), "CH4")


class WiringTests(unittest.TestCase):
    """The payload field and the client title must exist — that is the actual fix."""

    def setUp(self):
        self.mesh = io.open("/home/slofi/overmesh/mesh.py", encoding="utf-8").read()
        self.chat = io.open("/home/slofi/overmesh/routes/chat.py", encoding="utf-8").read()
        self.js = io.open("/home/slofi/overmesh/static/js/app.js", encoding="utf-8").read()

    def test_payload_carries_channel_name(self):
        self.assertIn('"channel_name": mt_channel_name(interface, channel),', self.mesh)

    def test_channel_list_endpoint_uses_the_same_helper(self):
        self.assertIn("mt_channel_name(iface, index)", self.chat)

    def test_client_title_uses_the_channel_name(self):
        self.assertIn("const title = data.is_dm ? `MT DM from ${data.from_name}` : `MT ${_mtChan}`;", self.js)
        self.assertIn("data.channel_name", self.js)

    def test_client_falls_back_before_showing_a_bare_index(self):
        self.assertIn("(chatChannels.find(c => c.index === data.channel) || {}).name", self.js)


if __name__ == "__main__":
    unittest.main()
