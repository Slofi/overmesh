"""Log retention (30 days, oldest first) and the Silent-Running suppression lines (S453).

Filip, 2026-10-16: *"Sure, add those to the logs to, just to be consistant.. also, when are the logs erased/for how
long are they retained? I would like something like kept for 30 days, than the oldest entry is deleted.. How is it
done now?"*

How it was done before this: every log-ish table was bounded by **count**, never by age — MT messages kept the
newest 1000, MC messages the newest 2000, traceroute history the newest 50, the in-app alert log the newest 150.
That caps size but not age, so on a busy mesh yesterday's conversation could be evicted while an idle month kept
everything for ever. Only passive observations had a time-based cleanup (30 days) and only when triggered by hand.

Now: an age-based prune runs on its own (shortly after boot, then every 6 hours) with a 30-day default, and it logs
what it removed. These tests pin the behaviour, the opt-out, the one deliberate exception, and — because a loop that
is never started is invisible, like the inert guard earlier the same day — that the loop is actually wired into the app.
"""
import json
import os
import sys
import tempfile
import time
import unittest
from unittest import mock
from pathlib import Path

_TEST_DIR = tempfile.TemporaryDirectory(prefix="overmesh-retention-")
_CONFIG_PATH = Path(_TEST_DIR.name) / "config.json"
_DATA_DIR = Path(_TEST_DIR.name) / "data"
_DATA_DIR.mkdir(parents=True, exist_ok=True)
_CONFIG_PATH.write_text(json.dumps({"nodes": [], "mc_nodes": [], "silent_mode": False}), encoding="utf-8")
os.environ.setdefault("OVERMESH_CONFIG", str(_CONFIG_PATH))
os.environ.setdefault("OVERMESH_DATA_DIR", str(_DATA_DIR))

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

import config  # noqa: E402
import db  # noqa: E402

DAY = 86400
RADIO = "mc_test_radio"


def _insert(conn, table, ts, **cols):
    """Insert one row with only the columns we care about, filling whatever the live
    schema requires (NOT NULL without a default) so this fixture cannot rot."""
    info = conn.execute("PRAGMA table_info(%s)" % table).fetchall()
    names = {r[1] for r in info}
    values = {"ts": ts}
    for col in info:
        name, notnull, default, pk = col[1], col[3], col[4], col[5]
        if notnull and default is None and not pk and name not in values:
            values[name] = "x"
    values.update({k: v for k, v in cols.items() if k in names})
    placeholders = ", ".join("?" * len(values))
    conn.execute("INSERT INTO %s (%s) VALUES (%s)" % (table, ", ".join(values), placeholders), list(values.values()))


def _count(conn, table, cutoff):
    return conn.execute("SELECT COUNT(*) FROM %s WHERE ts >= ?" % table, (cutoff,)).fetchone()[0]


class PruneLogsTests(unittest.TestCase):
    def setUp(self):
        db.init_prefs_db()                 # the schema is created by the app, not by get_prefs_db()
        self.now = int(time.time())
        self.old = self.now - 60 * DAY     # well past 30 days
        self.new = self.now - 1 * DAY      # inside the window

    def _connect_mt_radio(self, radio_id="mt_test"):
        """MT message DBs hang off the live connection, so the fixture registers one —
        which is also what makes the prune reachable at all (a radio that is not
        connected has no open DB, and its old rows wait until it connects again)."""
        path = str(Path(_DATA_DIR) / f"{radio_id}_msgs.db")
        db.init_msgs_db(path)
        with db.connections_lock:
            db.connections[radio_id] = {"msgs_db": path}
        self.addCleanup(lambda: db.connections.pop(radio_id, None))
        return path

    def test_old_traceroutes_and_mt_messages_go_and_recent_stay(self):
        with db.get_prefs_db() as conn:
            _insert(conn, "traceroute_history", self.old, id=9001, node_id="n1")
            _insert(conn, "traceroute_history", self.new, id=9002, node_id="n1")
        path = self._connect_mt_radio()
        conn = db._connect(path)
        _insert(conn, "messages", self.old, id="old-msg", text="old")
        _insert(conn, "messages", self.new, id="new-msg", text="new")
        conn.commit()
        conn.close()

        removed = db.prune_logs(30)

        self.assertEqual(removed.get("traceroutes"), 1, "exactly the one out-of-window traceroute")
        self.assertEqual(removed.get("messages:mt_test"), 1, "exactly the one out-of-window message")
        with db.get_prefs_db() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM traceroute_history WHERE id=9001").fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM traceroute_history WHERE id=9002").fetchone()[0], 1)
        conn = db._connect(path)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM messages WHERE id='old-msg'").fetchone()[0], 0)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM messages WHERE id='new-msg'").fetchone()[0], 1,
                         "the recent message must survive")
        conn.close()

    def test_mc_per_radio_logs_are_pruned_too(self):
        # The prune discovers MC radios the way the app does: from the config (the
        # registries are filled for configured radios at startup), so the fixture
        # must configure the radio rather than only create its files.
        patcher = mock.patch.dict(config.CONFIG, {"mc_nodes": [{"id": RADIO, "enabled": True}]})
        patcher.start()
        self.addCleanup(patcher.stop)
        with db.get_mc_msgs_db(RADIO) as conn:
            _insert(conn, "messages", self.old, id="mc-old", text="old")
            _insert(conn, "messages", self.new, id="mc-new", text="new")
        with db.get_mc_passive_db(RADIO) as conn:
            _insert(conn, "passive_obs", self.old, pubkey_pre="aa", obs_type="rx")
            _insert(conn, "passive_obs", self.new, pubkey_pre="aa", obs_type="rx")

        removed = db.prune_logs(30)

        self.assertEqual(removed.get("messages:" + RADIO), 1)
        self.assertEqual(removed.get("passive_obs:" + RADIO), 1)
        with db.get_mc_msgs_db(RADIO) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0], 1)
        with db.get_mc_passive_db(RADIO) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM passive_obs").fetchone()[0], 1)

    def test_zero_days_disables_the_prune(self):
        with db.get_prefs_db() as conn:
            _insert(conn, "traceroute_history", self.old, id=9003, node_id="n1")
        self.assertEqual(db.prune_logs(0), {}, "0 must mean keep everything")
        with db.get_prefs_db() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM traceroute_history WHERE id=9003").fetchone()[0], 1)

    def test_position_history_is_deliberately_excluded(self):
        """It is map track data with its own 1-year window, not a log. Excluded on
        purpose so the omission is a decision a reader can see, not an accident."""
        with db.get_prefs_db() as conn:
            _insert(conn, "node_position_history", self.now - 400 * DAY,
                    node_id="n1", radio_id="r1", lat=46.0, lon=14.0)
        db.prune_logs(30)
        with db.get_prefs_db() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM node_position_history").fetchone()[0], 1,
                             "a 400-day-old position must survive a 30-day log prune")

    def test_nothing_to_remove_returns_an_empty_dict(self):
        self.assertEqual(db.prune_logs(30), {}, "no old rows -> nothing reported, nothing logged")


class RetentionWiringTests(unittest.TestCase):
    """The loop must actually run, and the suppressions must be visible in the log."""

    def setUp(self):
        self.app_src = (REPO / "app.py").read_text(encoding="utf-8")
        self.db_src = (REPO / "db.py").read_text(encoding="utf-8")
        self.bot_src = (REPO / "bot.py").read_text(encoding="utf-8")

    def test_the_housekeeping_loop_is_started(self):
        self.assertIn("from db import log_retention_loop", self.app_src,
                      "the retention loop exists but is never started — it would be inert")
        self.assertIn("threading.Thread(target=log_retention_loop, daemon=True).start()", self.app_src)

    def test_the_loop_logs_what_it_removes(self):
        body = self.db_src[self.db_src.index("def log_retention_loop"):]
        body = body[:body.index("def cleanup_passive_obs")]
        self.assertIn("[retention] removed", body, "a silent prune cannot be told apart from a broken one")
        self.assertIn("log_retention_days", body, "the window must be configurable")

    def test_both_bot_suppressions_are_logged(self):
        for net in ("MT", "MC"):
            with self.subTest(network=net):
                self.assertIn("[bot] Silent Running active — %s bot command" % net, self.bot_src,
                              "a swallowed bot command must say so in the log")
        # and the silent check must be its OWN branch: merging it with the `enabled`
        # check is what made the suppression invisible in the first place
        self.assertNotIn('if not cfg.get("enabled") or CONFIG.get("silent_mode"):', self.bot_src)


if __name__ == "__main__":
    unittest.main()
