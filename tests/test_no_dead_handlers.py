"""Guard: every inline handler on OM's pages must point at a function that exists (2026-09-25).

Why this exists: the HD's Lite bug was a tap handler calling functions the 2026-06-24 overhaul had removed, and
nothing caught it for three months. A button that throws on tap looks exactly like a button that works - until
you tap it. This is the mechanical detector for that class, so it cannot happen quietly again.

It checks both pages against the JS they actually load (index.html -> app.js, lite.html -> lite.js), including
handler strings generated inside JS template literals: `onclick="selectChat(...)"` is text inside a backtick
string, so a template-only check never sees it.

The predicate requires the call NOT to be preceded by `.`, which is the bug in my first version of this sweep:
without it, `document.getElementById(...)` reports as a dead `getElementById()` and `Math.min(...)` as `min()` -
nine false findings, which is worse than none because it teaches you to skim the list. `_missing_handler_names`
carries its own positive control for that reason: the test below fails if the detector stops detecting.
"""
import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[1]
PAGES = {
    "templates/index.html": ["static/js/app.js"],
    "templates/lite.html": ["static/js/lite.js"],
}
IGNORE = {"event", "this", "window", "document", "return", "if", "javascript", "void", "console"}

# A handler's first called identifier, and NOT a method call: the lookbehind is the whole point.
_CALL = re.compile(r"(?<![.\w$])([A-Za-z_$][\w$]*)\s*\(")
_ON = re.compile(r"on(?:click|change|input|submit|keydown|keyup|blur|focus|pointerup|pointerdown)\s*=\s*([\"'])(.*?)\1", re.S)


def _defined_names(js: str) -> set[str]:
    out: set[str] = set()
    for pat in (
        r"function\s+([A-Za-z_$][\w$]*)\s*\(",
        r"window\.([A-Za-z_$][\w$]*)\s*=",
        r"(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=",
        r"([A-Za-z_$][\w$]*)\s*[:=]\s*(?:async\s*)?\(",
    ):
        out.update(re.findall(pat, js))
    return out


def _missing_handler_names(text: str, defined: set[str]) -> set[str]:
    """Handler names in `text` whose function is not in `defined` (and is not a method call)."""
    missing: set[str] = set()
    for _q, body in _ON.findall(text):
        m = _CALL.search(body)
        if not m:
            continue
        name = m.group(1)
        if name in IGNORE or name.startswith("_"):
            continue
        if name not in defined:
            missing.add(name)
    return missing


def test_the_detector_can_still_detect() -> None:
    # A check that has never seen a failure is not a check: this one must flag a handler with no definition...
    assert _missing_handler_names('onclick="_totallyMissingForControl()"', set()) == {"_totallyMissingForControl"}
    # ...and must NOT flag a method call, which is the false positive that made nine bogus findings.
    assert _missing_handler_names('onclick="Math.min(1,2)"', set()) == set()
    assert _missing_handler_names('onclick="leafletMap.closePopup()"', set()) == set()


def test_no_page_has_a_dead_inline_handler() -> None:
    problems: list[str] = []
    for page, jsfiles in PAGES.items():
        html = (ROOT / page).read_text(encoding="utf-8")
        js = "".join((ROOT / f).read_text(encoding="utf-8") for f in jsfiles)
        defined = _defined_names(js)
        # handlers written in the page, and handlers generated inside its JS
        dead = _missing_handler_names(html, defined) | _missing_handler_names(js, defined)
        if dead:
            problems.append(f"{page}: " + ", ".join(sorted(dead)))
    assert not problems, "inline handlers with no definition on their page: " + " | ".join(problems)
