"""Log retention: count caps bound history, and the one uncapped store is age-pruned (S453).

Filip, 2026-10-16: *"when are the logs erased/for how long are they retained? I would like something like kept for 30
days, than the oldest entry is deleted.. How is it done now?"* — answered, built, and then revised by his follow-up:
*"Ow, I see.. So maybe those 30 days are to short. We can stick with those 1000 entries; how large is that in kb/mb?"*

What the measurement showed (VACUUMed copies of his live DBs): 416 bytes per MT message, 659 per MC message, 162 per
passive observation — so 1000 messages is ~400 KB per radio, and age-pruning his history freed a few hundred kilobytes on
a hub with 192 GB free. The COUNT caps were always the meaningful bound (they also bound what the UI renders).

The rule now, and what these tests pin:
  * messages (1000 MT / 2000 MC per radio), traceroute history (50) and the in-app alert log (150) — bounded by COUNT in
    their own save paths; the age prune must NOT touch them, so a 30-day window cannot quietly delete history;
  * passive observations — no count cap, so an age window (30 days by default, `app.log_retention_days`, 0 disables) runs
    on its own, logging what it removed;
  * `node_position_history` — 1-year window, map track data rather than a log, deliberately outside both rules."""

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
        """MT message DBs hang off the live connection, so the fixture registers one."""
        path = str(Path(_DATA_DIR) / f"{radio_id}_msgs.db")
        db.init_msgs_db(path)
        with db.connections_lock:
            db.connections[radio_id] = {"msgs_db": path}
        self.addCleanup(lambda: db.connections.pop(radio_id, None))
        return path

    def test_the_age_prune_does_not_touch_messages_or_traceroutes(self):
        """His follow-up in one sentence: 30 days was too short, the entry cap is the rule.
        The age prune must leave count-bounded stores alone — that is the whole change."""
        with db.get_prefs_db() as conn:
            _insert(conn, "traceroute_history", self.old, id=9001, node_id="n1")
        path = self._connect_mt_radio()
        conn = db._connect(path)
        _insert(conn, "messages", self.old, id="old-msg", text="old")
        conn.commit()
        conn.close()

        removed = db.prune_logs(30)

        self.assertEqual(removed, {}, "messages and traceroutes are bounded by count, not age")
        with db.get_prefs_db() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM traceroute_history WHERE id=9001").fetchone()[0], 1)
        conn = db._connect(path)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM messages WHERE id='old-msg'").fetchone()[0], 1,
                         "a 60-day-old message must survive: the 1000-entry cap is what bounds history")
        conn.close()

    def test_the_count_cap_is_what_bounds_message_history(self):
        """The load-bearing bound, so it gets a real test: save more than the cap and
        the oldest entries go while the newest stay."""
        path = self._connect_mt_radio()
        for i in range(1005):
            db.save_message({"id": "m%04d" % i, "radio_id": "mt_test", "text": "x",
                             "ts": self.now - (1005 - i), "channel": 0})
        conn = db._connect(path)
        rows = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
        self.assertEqual(rows, 1000, "the MT message cap is 1000 entries")
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM messages WHERE id='m0000'").fetchone()[0], 0,
                         "the oldest entries are the ones dropped")
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM messages WHERE id='m1004'").fetchone()[0], 1,
                         "the newest entry survives")
        conn.close()

    def test_mc_passive_observations_are_age_pruned(self):
        """The one store with no count cap: it is what the age window is for."""
        patcher = mock.patch.dict(config.CONFIG, {"mc_nodes": [{"id": RADIO, "enabled": True}]})
        patcher.start()
        self.addCleanup(patcher.stop)
        with db.get_mc_passive_db(RADIO) as conn:
            _insert(conn, "passive_obs", self.old, pubkey_pre="aa", obs_type="rx")
            _insert(conn, "passive_obs", self.new, pubkey_pre="aa", obs_type="rx")
        with db.get_mc_msgs_db(RADIO) as conn:
            _insert(conn, "messages", self.old, id="mc-old", text="old")

        removed = db.prune_logs(30)

        self.assertEqual(removed.get("passive_obs:" + RADIO), 1)
        self.assertNotIn("messages:" + RADIO, removed, "MC messages are capped by count, not by age")
        with db.get_mc_passive_db(RADIO) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM passive_obs WHERE ts = ?", (self.old,)).fetchone()[0], 0,
                             "the out-of-window observation is gone")
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM passive_obs WHERE ts = ?", (self.new,)).fetchone()[0], 1,
                             "the recent observation survives")
        with db.get_mc_msgs_db(RADIO) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0], 1)

    def test_zero_days_disables_the_prune(self):
        patcher = mock.patch.dict(config.CONFIG, {"mc_nodes": [{"id": RADIO, "enabled": True}]})
        patcher.start()
        self.addCleanup(patcher.stop)
        with db.get_mc_passive_db(RADIO) as conn:
            _insert(conn, "passive_obs", self.old, pubkey_pre="aa", obs_type="rx")
        self.assertEqual(db.prune_logs(0), {}, "0 must mean keep everything")
        with db.get_mc_passive_db(RADIO) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM passive_obs WHERE ts = ?", (self.old,)).fetchone()[0], 1,
                             "with the window disabled even an out-of-window row stays")

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
        patcher = mock.patch.dict(config.CONFIG, {"mc_nodes": [{"id": "mc_empty_radio", "enabled": True}]})
        patcher.start()
        self.addCleanup(patcher.stop)
        with db.get_mc_passive_db("mc_empty_radio"):
            pass          # create it, empty
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
