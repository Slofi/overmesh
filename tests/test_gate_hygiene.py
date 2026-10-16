"""Guards for the test gate itself (S453).

Why this file exists: on 2026-10-16 the map-key guard (tests/test_no_bundled_map_key.py) turned out to
have contributed ZERO tests since the day it was written. It was pytest-style — bare `def test_*`
functions at module level — while tests/run.sh runs `unittest discover`, which silently ignores those.
Formatted like a guard, named like a guard, and completely inert: nobody got a red test, and the only
reason it surfaced is that someone read the file and asked why the suite did not move.

A test that cannot fail is worse than no test, because it buys confidence it has not earned. So the gate
now checks its own hygiene:

  1. every tests/*.js must be listed in tests/run.sh (a new JS test is not run otherwise);
  2. every tests/test_*.py must contribute at least one collected test;
  3. no tests/*.py may carry module-level `def test_*` functions, the exact shape unittest ignores;
  4. every tests/*.py must be either a test module (test_*.py) or this repo's known helper, so a stray
     "check_all_the_things.py" cannot sit there looking like it runs;
  5. every JS test listed in run.sh must exist on disk (a rename must not leave a silent `node` failure
     skipped by a `|| true`-style shell shrug).
"""

import collections
import pathlib
import re
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
TESTS = ROOT / "tests"
RUN_SH = (TESTS / "run.sh").read_text(encoding="utf-8")

# Files in tests/ that are deliberately not test modules.
ALLOWED_NON_TEST_FILES = {"run.sh"}


def _js_files() -> set[str]:
    return {p.name for p in TESTS.glob("*.js")}


def _js_files_run() -> set[str]:
    return {pathlib.Path(m).name for m in re.findall(r"^\s*node\s+(tests/[A-Za-z0-9_./-]+\.js)", RUN_SH, re.M)}


class GateHygieneTests(unittest.TestCase):
    def test_every_js_test_is_listed_in_run_sh(self) -> None:
        missing = sorted(_js_files() - _js_files_run())
        self.assertFalse(
            missing,
            "these JS tests exist but tests/run.sh never runs them (add a `node tests/<file>` line): "
            + ", ".join(missing),
        )

    def test_every_listed_js_test_exists(self) -> None:
        ghost = sorted(_js_files_run() - _js_files())
        self.assertFalse(ghost, "tests/run.sh runs files that do not exist: " + ", ".join(ghost))

    def test_every_python_test_module_contributes_tests(self) -> None:
        """unittest discover imports test_*.py and collects TestCase methods; a module with
        none is imported, reported as fine, and contributes nothing."""
        suite = unittest.TestLoader().discover(str(TESTS), pattern="test_*.py")
        counts: collections.Counter = collections.Counter()

        def walk(s):
            for item in s:
                if isinstance(item, unittest.TestSuite):
                    walk(item)
                else:
                    counts[item.__class__.__module__.split(".")[-1]] += 1

        walk(suite)
        silent = sorted(p.stem for p in TESTS.glob("test_*.py") if counts[p.stem] == 0)
        self.assertFalse(silent, "these test modules contribute ZERO tests to the gate: " + ", ".join(silent))

    def test_no_module_level_pytest_style_test_functions(self) -> None:
        """The exact defect: `def test_x(): ...` at module level in a test_*.py is invisible to
        unittest discover. Either make it a TestCase method, or run the file with pytest — but then
        the gate has to invoke pytest, and today it does not."""
        offenders = []
        for path in sorted(TESTS.glob("*.py")):
            for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                if re.match(r"^def test_[A-Za-z0-9_]*\(", line):
                    offenders.append(f"{path.name}:{n}")
        self.assertFalse(
            offenders,
            "module-level `def test_*` is not collected by `unittest discover` (make it a TestCase, "
            "or add pytest to the gate deliberately): " + ", ".join(offenders),
        )

    def test_every_file_in_tests_is_known(self) -> None:
        """A stray file that looks like a test but is never run is the same trap in a different shape."""
        unknown = sorted(
            p.name for p in TESTS.iterdir()
            if p.is_file()
            and not p.name.startswith("test_")
            and p.suffix in {".py", ".js"}
            and p.name not in ALLOWED_NON_TEST_FILES
        )
        self.assertFalse(
            unknown,
            "these tests/ files are neither test_*.py nor listed helpers, so nothing runs them: "
            + ", ".join(unknown),
        )


if __name__ == "__main__":
    unittest.main()
