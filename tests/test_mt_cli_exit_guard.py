import unittest


class MtCliExitGuardTests(unittest.TestCase):
    """meshtastic's our_exit() raises SystemExit from code that runs inside the
    serial reader thread; the reader only catches Exception, so the link died
    silently (seen as 'Sense makes radios disconnect'). Our guard must turn that
    into a catchable error. Armed so the failing case is reachable."""

    def test_our_exit_is_replaced_and_raises_catchable_error(self):
        import mesh  # noqa: F401  (installs the guard at import)
        import meshtastic.mesh_interface as mi

        self.assertTrue(getattr(mi.our_exit, "__name__", "") == "_om_suppress_cli_exit",
                        "guard not installed on meshtastic.mesh_interface.our_exit")
        with self.assertRaises(RuntimeError):
            mi.our_exit("No response from node. At least firmware 2.1.22 is required.")

    def test_system_exit_cannot_escape_a_reader_style_loop(self):
        """Mimic the library reader: except Exception around a handler call.
        Without the guard this raises SystemExit (escapes); with it, caught."""
        import mesh  # noqa: F401
        import meshtastic.mesh_interface as mi

        survived = False
        try:
            try:
                mi.our_exit("simulated NO_RESPONSE NAK")
            except Exception:
                survived = True   # reader logs and keeps going
        except SystemExit:
            self.fail("SystemExit escaped the reader-style handler - guard not effective")
        self.assertTrue(survived)


if __name__ == "__main__":
    unittest.main()
