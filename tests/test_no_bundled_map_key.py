"""No map key may ship inside OverMesh's own JS, and the CARTO layers must take the user's (2026-09-25).

This is the mesh-torry guard's counterpart, and it exists because OverMesh was the app the shared-key sweep
MISSED: the inventory listed `overmesh-lite` and the sweep grepped `~/Projects`, while the live OM lives at
`~/overmesh` (and `/home/gandalf/overmesh`). So OM kept a hardcoded CARTO key in five layer URLs, and when that
key was rotated at CARTO the five layers silently became HTTP 403 — no tiles at all.

Two invariants, both of which the code now satisfies and neither of which may drift back:
  1. no CARTO basemap key is written in plain text anywhere under static/js;
  2. every cartocdn URL carries the `{cartokey}` placeholder instead, which _resolveTileUrl() fills from
     localStorage — so the key is the operator's own, entered in Settings, never bundled.

Run: python3 -m pytest tests/test_no_bundled_map_key.py
"""

import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[1]
JS_DIR = ROOT / "static" / "js"

# A CARTO basemap key looks like cb1_28gv_2_<hex>. Deliberately loose about the middle so a future
# key format is still caught: anything matching cb1_<word>_<word>_<long hex>.
CARTO_KEY = re.compile(r"cb1_[A-Za-z0-9]+_[A-Za-z0-9]+_[0-9a-f]{16,}", re.IGNORECASE)


def _js_files() -> list[pathlib.Path]:
    return sorted(JS_DIR.glob("*.js"))


def test_no_carto_key_is_shipped_in_any_js() -> None:
    hits: list[str] = []
    for path in _js_files():
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if CARTO_KEY.search(line):
                hits.append(f"{path.name}:{n}")
    assert not hits, "a CARTO key is bundled in shipped JS (it must be the user's own): " + ", ".join(hits)


def test_every_cartocdn_tile_url_uses_the_placeholder() -> None:
    """A cartocdn URL without `{cartokey}` is either keyless (a watermark) or hardcoded (forbidden)."""
    offenders: list[str] = []
    for path in _js_files():
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if "basemaps.cartocdn.com" in line and "{cartokey}" not in line:
                offenders.append(f"{path.name}:{n}")
    assert not offenders, "cartocdn layer without the {cartokey} placeholder: " + ", ".join(offenders)
