import unittest


class MtCliExitGuardTests(unittest.TestCase):
    """meshtastic's our_exit() raises SystemExit from code that runs inside the
    serial reader thread; the reader only catches Exception, so the link died
    silently (seen as 'Sense makes radios disconnect'). The guard must cover
    every module that binds our_exit by name. Armed so failures are reachable."""

    def test_guard_covers_every_module_that_binds_our_exit(self):
        import mesh  # noqa: F401  (installs the guard at import)
        import importlib

        for modname in ("meshtastic.mesh_interface", "meshtastic.node",
                        "meshtastic.remote_hardware", "meshtastic.serial_interface",
                        "meshtastic.util"):
            mod = importlib.import_module(modname)
            if not hasattr(mod, "our_exit"):
                continue
            self.assertEqual(getattr(mod.our_exit, "__name__", ""), "_om_suppress_cli_exit",
                             f"unguarded our_exit in {modname}")

    def test_our_exit_raises_catchable_error_not_system_exit(self):
        import mesh  # noqa: F401
        import meshtastic.mesh_interface as mi

        with self.assertRaises(RuntimeError):
            mi.our_exit("No response from node. At least firmware 2.1.22 is required.")

    def test_system_exit_cannot_escape_a_reader_style_loop(self):
        """Mimic the library reader: except Exception around a handler call.
        Without the guard this raises SystemExit (escapes, silent death)."""
        import mesh  # noqa: F401
        import meshtastic.mesh_interface as mi

        survived = False
        try:
            try:
                mi.our_exit("simulated NO_RESPONSE NAK")
            except Exception:
                survived = True   # reader logs it and keeps going
        except SystemExit:
            self.fail("SystemExit escaped the reader-style handler - guard not effective")
        self.assertTrue(survived)


if __name__ == "__main__":
    unittest.main()
