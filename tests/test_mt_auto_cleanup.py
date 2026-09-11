import unittest
from unittest import mock

import routes.nodes as nodes_mod
import config as config_mod


class MtAutoCleanupPassTests(unittest.TestCase):
    """The MT auto stale-purge pass has never run live (feature ships OFF).
    Arm its logic with mocks so the behaviour is actually checked:
    only enabled radios, days clamped, per-radio filtering, eviction calls."""

    def setUp(self):
        self._orig_nodes = config_mod.CONFIG.get("nodes")
        config_mod.CONFIG["nodes"] = [
            {"id": "mtA", "auto_cleanup": True,  "auto_cleanup_days": 30},
            {"id": "mtB", "auto_cleanup": True,  "auto_cleanup_days": 3},   # clamps to 7
            {"id": "mtC", "auto_cleanup": False, "auto_cleanup_days": 30},  # untouched
        ]
        self.evicted = []

    def tearDown(self):
        if self._orig_nodes is None:
            config_mod.CONFIG.pop("nodes", None)
        else:
            config_mod.CONFIG["nodes"] = self._orig_nodes

    def _run(self, stale):
        with mock.patch.object(nodes_mod, "_collect_stale_nodes", return_value=stale), \
             mock.patch.object(nodes_mod, "_mt_auto_evict_node",
                               side_effect=lambda rid, nid, keep_om_row=False: (self.evicted.append((rid, nid)) or True)):
            return nodes_mod.run_mt_auto_cleanup_once()

    def test_only_enabled_radios_are_processed_and_filtered_by_radio(self):
        stale = [{"id": "n1", "radio_id": "mtA"}, {"id": "n2", "radio_id": "mtB"},
                 {"id": "n3", "radio_id": "mtC"}]   # mtC disabled -> must be ignored
        res = self._run(stale)
        self.assertIn("mtA", res)
        self.assertIn("mtB", res)
        self.assertNotIn("mtC", res)
        self.assertNotIn(("mtC", "n3"), self.evicted)
        self.assertEqual(sorted(self.evicted), [("mtA", "n1"), ("mtB", "n2")])
        self.assertEqual(res["mtA"], {"candidates": 1, "removed": 1})

    def test_days_are_clamped_to_sane_bounds(self):
        seen_cutoffs = []

        def fake_collect(cutoff):
            seen_cutoffs.append(cutoff)
            return []

        with mock.patch.object(nodes_mod, "_collect_stale_nodes", side_effect=fake_collect), \
             mock.patch.object(nodes_mod, "_mt_auto_evict_node", return_value=True):
            nodes_mod.run_mt_auto_cleanup_once()

        import time
        now = int(time.time())
        # two enabled radios -> two cutoffs; 30d and 3d-clamped-to-7d
        gaps = sorted(round((now - c) / 86400) for c in seen_cutoffs)
        self.assertEqual(gaps, [7, 30])

    def test_no_enabled_radios_means_no_work(self):
        config_mod.CONFIG["nodes"] = [{"id": "mtC", "auto_cleanup": False}]
        with mock.patch.object(nodes_mod, "_collect_stale_nodes") as collect, \
             mock.patch.object(nodes_mod, "_mt_auto_evict_node") as evict:
            res = nodes_mod.run_mt_auto_cleanup_once()
        collect.assert_not_called()
        evict.assert_not_called()
        self.assertEqual(res, {})

    def test_collect_failure_is_reported_not_fatal(self):
        with mock.patch.object(nodes_mod, "_collect_stale_nodes", side_effect=RuntimeError("boom")), \
             mock.patch.object(nodes_mod, "_mt_auto_evict_node") as evict:
            res = nodes_mod.run_mt_auto_cleanup_once()
        evict.assert_not_called()
        self.assertIn("error", res.get("mtA", {}))


if __name__ == "__main__":
    unittest.main()
