"""The /lite shell must not be cacheable — added 2026-09-26, with the fix.

/lite carried NO Cache-Control header while / has had `no-store, must-revalidate`
since 2026-09-10, so whether the Hand-Deck kiosk re-read the shell was a browser
lottery. The page carries the version-stamped asset URLs, so a cached copy can pin
a stale ?v= — the same failure index()'s comment describes.

Kept deliberately as a pair with the / assertion: the bug was that the two routes
drifted apart, so a check on only one of them would not have caught it.
"""
import unittest

import app as app_module

EXPECTED = "no-store, must-revalidate"


class ShellCacheHeaderTests(unittest.TestCase):
    def setUp(self):
        self.client = app_module.app.test_client()

    def test_index_is_no_store(self):
        r = self.client.get("/")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.headers.get("Cache-Control"), EXPECTED)

    def test_lite_is_no_store(self):
        r = self.client.get("/lite")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.headers.get("Cache-Control"), EXPECTED)

    def test_both_shells_send_pragma_no_cache(self):
        for path in ("/", "/lite"):
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path).headers.get("Pragma"), "no-cache")


if __name__ == "__main__":
    unittest.main()
