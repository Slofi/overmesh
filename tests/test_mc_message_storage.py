import tempfile
import unittest
from unittest import mock

import db


class McMessageStorageTest(unittest.TestCase):
    def test_save_load_dedupe_and_delete_channel_messages(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(db, "DATA_DIR", tmp):
                msg = {
                    "id": "mc-test-1",
                    "type": "mc_message",
                    "radio_id": "mc_test",
                    "radio_name": "MC Test",
                    "network": "mc",
                    "subtype": "channel",
                    "channel": 2,
                    "from_id": "abcd1234",
                    "text": "hello",
                    "ts": 100,
                    "sent": False,
                    "path": "aabb",
                    "path_len": 1,
                    "path_hash_size": 2,
                    "rx_snr": -7.5,
                }

                db.save_mc_message(msg)
                db.save_mc_message(dict(msg))

                rows = db.load_mc_messages("mc_test")
                self.assertEqual(len(rows), 1)
                self.assertEqual(rows[0]["type"], "mc_message")
                self.assertEqual(rows[0]["network"], "mc")
                self.assertEqual(rows[0]["radio_id"], "mc_test")
                self.assertEqual(rows[0]["subtype"], "channel")
                self.assertEqual(rows[0]["channel"], 2)
                self.assertEqual(rows[0]["text"], "hello")
                self.assertFalse(rows[0]["sent"])
                self.assertEqual(rows[0]["path"], "aabb")
                self.assertEqual(rows[0]["path_len"], 1)
                self.assertEqual(rows[0]["path_hash_size"], 2)
                self.assertEqual(rows[0]["rx_snr"], -7.5)

                removed = db.delete_mc_channel_messages("mc_test", 2)
                self.assertEqual(removed, 1)
                self.assertEqual(db.load_mc_messages("mc_test"), [])


if __name__ == "__main__":
    unittest.main()
