"""Guard: OM transmits an MC advert ONLY on an explicit request (Filip, S453, 2026-10-16).

Filip, after seeing ERA-3's advert on map.meshcore.si: *"I dont want OM to advert without
me telling it to do so."* Every automatic advert path was removed on that order — the
startup flood advert, both ping/status pre-adverts, the contact-import advert, and the
re-adverts after a coords or share-position change. What is left is the Advert button,
Scan, and a managed repeater's own Local/Flood advert.

This is a deliberately *structural* guard: the policy is "never, from anywhere else", and
allowlisting the call sites is the only way to assert that negative across modules this
large. It fails if an advert transmit call appears in a function outside the allowlist,
so a later change cannot quietly reintroduce an unprompted mesh-wide transmission.

Armed when written: adding `await mc.commands.send_advert(flood=True)` inside
`_import_contact_async` makes it fail (and removing it makes it pass again).
"""
import ast
import pathlib
import unittest

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent

# The only functions allowed to contain an advert transmit call, anywhere in the app:
#   _send_advert_async / _do_advert  — the TX primitive and its detached task
#   send_advert                      — the sync wrapper the routes call
#   api_mc_advert / api_mc_scan      — the two explicit user actions
ALLOWED_CALLERS = {"_send_advert_async", "_do_advert", "send_advert", "api_mc_advert", "api_mc_scan"}

ADVERT_CALL_NAMES = {"send_advert", "_send_advert_async"}


def advert_callers(path):
    """Map enclosing function name → line numbers of advert TX calls inside it.

    Nested functions are attributed to their outer function too (a call inside a nested
    task is still inside that function's source), which is why both _do_advert and
    _send_advert_async appear in the allowlist.
    """
    tree = ast.parse(path.read_text())
    callers = {}
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for sub in ast.walk(node):
            if not isinstance(sub, ast.Call):
                continue
            fn = sub.func
            name = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", None)
            if name in ADVERT_CALL_NAMES:
                callers.setdefault(node.name, []).append(sub.lineno)
    return callers


def source_files():
    """Every app module (repo root + routes/) — the whole surface that could transmit."""
    return sorted(list(REPO_ROOT.glob("*.py")) + list((REPO_ROOT / "routes").glob("*.py")))


class AdvertsAreManualOnlyTests(unittest.TestCase):
    def test_no_advert_site_outside_the_allowlist(self):
        offenders = {}
        scanned = 0
        for path in source_files():
            scanned += 1
            for name, lines in advert_callers(path).items():
                if name not in ALLOWED_CALLERS:
                    offenders[f"{path.name}:{name}"] = lines
        self.assertGreater(scanned, 5, "the scan must actually have files to look at")
        self.assertEqual(
            offenders, {},
            "an MC advert would be transmitted from a non-allowlisted function — adverts are "
            "manual-only (Filip, S453): the Advert button, Scan, or a managed repeater's own "
            "advert. Reintroducing an automatic advert needs his approval first.",
        )

    def test_the_allowlist_is_not_vacuous(self):
        """Positive control: if the allowed transmit sites vanished, the guard above checks nothing."""
        callers = {}
        for path in source_files():
            callers.update({f"{path.name}:{name}": lines for name, lines in advert_callers(path).items()})
        self.assertIn("mesh_mc.py:_send_advert_async", callers, "the advert TX primitive must still exist")
        self.assertIn("mesh_mc.py:send_advert", callers, "the advert wrapper must still exist")
        self.assertIn("mc.py:api_mc_advert", callers, "the Advert button must still transmit")
        self.assertIn("mc.py:api_mc_scan", callers, "Scan must still transmit its advert")


if __name__ == "__main__":
    unittest.main()
