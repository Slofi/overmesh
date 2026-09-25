"""Guard: every inline handler on OM's pages must point at a function that exists (2026-09-25).

Why this exists: the HD's Lite bug was a tap handler calling functions the 2026-06-24 overhaul had removed, and
nothing caught it for three months. A button that throws on tap looks exactly like a button that works - until you
tap it. This is the mechanical detector for that class.

It checks both pages against the JS they actually load (index.html -> app.js, lite.html -> lite.js), including the
handler strings generated inside JS template literals: `onclick="selectChat(...)"` is text inside a backtick
string, so a template-only check never sees it.

Three things this deliberately does, each of which was a false positive in an earlier version of the sweep:
  * it strips comments before scanning - `fn('VALUE')` in app.js is inside a `//` comment explaining how a helper
    escapes quotes, not a handler;
  * it ignores names the browser or a vendored library provides (setTimeout, alert, L, ...) - a handler doing
    `setTimeout(() => ..., 100)` is calling a built-in;
  * the call must NOT be preceded by a '.', so `document.getElementById(...)` is not reported as a dead
    `getElementById()` (nine such findings in the first version).

test_the_detector_can_still_detect() is the positive control: it fails if this ever stops detecting anything, so a
clean result keeps meaning something. Written as unittest because that is what tests/run.sh discovers
(`python3 -m unittest discover -s tests -p 'test_*.py'`) - the first version was pytest-style, so the repo's own
suite never ran it and a red test reached the remote. That is the failure this comment is really about.
"""

import pathlib
import re
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
PAGES = {
    "templates/index.html": ["static/js/app.js"],
    "templates/lite.html": ["static/js/lite.js"],
}

# Provided by the browser, by a library the page loads, or statement keywords - never functions of this app.
BUILTINS = {
    "setTimeout", "clearTimeout", "setInterval", "clearInterval", "requestAnimationFrame", "cancelAnimationFrame",
    "requestIdleCallback", "alert", "confirm", "prompt", "fetch", "parseInt", "parseFloat", "isNaN",
    "encodeURIComponent", "decodeURIComponent", "encodeURI", "decodeURI", "structuredClone", "getComputedStyle",
    "matchMedia", "btoa", "atob", "Number", "String", "Boolean", "Array", "Object", "Math", "JSON", "Date", "Error",
    "Promise", "Set", "Map", "event", "this", "window", "document", "navigator", "location", "history", "console",
    "return", "if", "for", "while", "switch", "typeof", "function", "void", "new", "try", "catch", "javascript",
    "L", "io", "Chart", "QRCode", "maplibregl",
}

_CALL = re.compile(r"(?<![.\w$])([A-Za-z_$][\w$]*)\s*\(")
_ON = re.compile(
    r"on(?:click|change|input|submit|keydown|keyup|blur|focus|pointerup|pointerdown)\s*=\s*([\"'])(.*?)\1",
    re.S,
)


def strip_comments(src: str) -> str:
    src = re.sub(r"<!--.*?-->", "", src, flags=re.S)      # HTML comments
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.S)       # block comments
    src = re.sub(r"(?m)^\s*//.*$", "", src)               # full-line JS comments only (URLs contain //)
    return src


def defined_names(js: str) -> set[str]:
    js = strip_comments(js)
    out: set[str] = set()
    for pat in (
        r"function\s+([A-Za-z_$][\w$]*)\s*\(",
        r"window\.([A-Za-z_$][\w$]*)\s*=",
        r"(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=",
        r"([A-Za-z_$][\w$]*)\s*[:=]\s*(?:async\s*)?\(",
    ):
        out.update(re.findall(pat, js))
    return out


def missing_handler_names(text: str, defined: set[str]) -> set[str]:
    """Handler names in `text` whose function is not defined and is not a built-in or a method call."""
    missing: set[str] = set()
    for _q, body in _ON.findall(strip_comments(text)):
        m = _CALL.search(body)
        if not m:
            continue
        name = m.group(1)
        if name in BUILTINS or name in defined:
            continue
        missing.add(name)
    return missing


class TestNoDeadHandlers(unittest.TestCase):
    def test_the_detector_can_still_detect(self) -> None:
        # Positive control: a handler with no definition must be flagged...
        self.assertEqual(
            missing_handler_names('onclick="totallyMissingForControl()"', set()), {"totallyMissingForControl"}
        )
        # ...a method call must not...
        self.assertEqual(missing_handler_names("onclick=\"document.getElementById('x').value=''\"", set()), set())
        self.assertEqual(missing_handler_names('onclick="leafletMap.closePopup()"', set()), set())
        # ...a built-in must not...
        self.assertEqual(missing_handler_names('onclick="setTimeout(() => f(), 100)"', set()), set())
        # ...and a commented-out example must not: this exact line was a false finding.
        self.assertEqual(
            missing_handler_names(
                '// Safe for embedding in onclick="fn(\'VALUE\')"\nonclick="realOne()"', {"realOne"}
            ),
            set(),
        )

    def test_no_page_has_a_dead_inline_handler(self) -> None:
        problems: list[str] = []
        for page, jsfiles in PAGES.items():
            html = (ROOT / page).read_text(encoding="utf-8")
            js = "".join((ROOT / f).read_text(encoding="utf-8") for f in jsfiles)
            defined = defined_names(js)
            # handlers written in the page, and handlers generated inside its JS
            dead = missing_handler_names(html, defined) | missing_handler_names(js, defined)
            if dead:
                problems.append(f"{page}: " + ", ".join(sorted(dead)))
        self.assertEqual(problems, [], "inline handlers with no definition on their page: " + " | ".join(problems))


if __name__ == "__main__":
    unittest.main()
