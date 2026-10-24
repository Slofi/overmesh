"""No map key may ship inside OverMesh's own JS, and every keyed provider must be complete.

Two things live here:

1. The bundled-key invariant (2026-09-25). It exists because OverMesh was the app the shared-key sweep
   MISSED: the inventory listed `overmesh-lite` and the sweep grepped `~/Projects`, while the live OM lives at
   `~/overmesh` (and `/home/gandalf/overmesh`). So OM kept a hardcoded CARTO key in five layer URLs, and when
   that key was rotated at CARTO the five layers silently became HTTP 403 — no tiles at all.

2. The provider-wiring invariant (2026-10-16). Filip: "Some are missing the API entry field in the settings."
   The Stadia Maps layers (Toner Lite/Dark, Stamen Terrain, Stadia Outdoors) needed a key — Stadia allows
   keyless use from localhost only — but had no placeholder, no settings field, no resolve step and no
   "key missing" guard, so on a domain-reached instance they failed with nothing to configure. That class of
   gap is now a test failure rather than something a user has to notice.

HOW THESE RUN (2026-10-16): this file was written with bare `test_*` functions, which `unittest discover` does
NOT collect — and `tests/run.sh` runs unittest, not pytest (pytest is not installed in the venv). So it
contributed ZERO tests to the gate and had never actually run. It is a TestCase now, which both unittest and
pytest collect.
"""

import pathlib
import re
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
JS_DIR = ROOT / "static" / "js"

# A CARTO basemap key looks like cb1_28gv_2_<hex>. Deliberately loose about the middle so a future
# key format is still caught: anything matching cb1_<word>_<word>_<long hex>.
CARTO_KEY = re.compile(r"cb1_[A-Za-z0-9]+_[A-Za-z0-9]+_[0-9a-f]{16,}", re.IGNORECASE)

# placeholder, localStorage key,      input id,              save handler
PROVIDERS = [
    ("apikey", "thunderforestApiKey", "tf-api-key-input", "saveThunderforestKey"),
    ("mtapikey", "mapTilerApiKey", "mt-api-key-input", "saveMapTilerKey"),
    ("cartokey", "cartoApiKey", "carto-api-key-input", "saveCartoKey"),
    ("stadiakey", "stadiaApiKey", "stadia-api-key-input", "saveStadiaKey"),
]


def _js_files() -> list[pathlib.Path]:
    return sorted(JS_DIR.glob("*.js"))


class NoBundledMapKeyTests(unittest.TestCase):
    def test_no_carto_key_is_shipped_in_any_js(self) -> None:
        hits: list[str] = []
        for path in _js_files():
            for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                if CARTO_KEY.search(line):
                    hits.append(f"{path.name}:{n}")
        self.assertFalse(hits, "a CARTO key is bundled in shipped JS (it must be the user's own): " + ", ".join(hits))

    def test_every_cartocdn_tile_url_uses_the_placeholder(self) -> None:
        """A cartocdn URL without `{cartokey}` is either keyless (a watermark) or hardcoded (forbidden)."""
        offenders: list[str] = []
        for path in _js_files():
            for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                # Only a TILE URL is in scope. A provider-name mention (e.g. the shared-layer credit
                # helper matching `source.includes('basemaps.cartocdn.com')`) is not a URL and must
                # not be flagged: the old line-only pattern flagged it the moment that helper landed.
                if ("basemaps.cartocdn.com" in line and ("https://" in line or "url:" in line)
                        and "{cartokey}" not in line):
                    offenders.append(f"{path.name}:{n}")
        self.assertFalse(offenders, "cartocdn layer without the {cartokey} placeholder: " + ", ".join(offenders))


class MapKeyProviderWiringTests(unittest.TestCase):
    """A keyed provider is only usable if every piece exists: placeholder, field,
    save handler, storage name, resolver, guard *and* the prefill."""

    def setUp(self):
        self.app = (JS_DIR / "app.js").read_text(encoding="utf-8")
        self.lite = (JS_DIR / "lite.js").read_text(encoding="utf-8")
        self.html = (ROOT / "templates" / "index.html").read_text(encoding="utf-8")

    def test_every_keyed_provider_is_wired_end_to_end(self) -> None:
        for placeholder, storage, input_id, save_fn in PROVIDERS:
            ph = "{" + placeholder + "}"
            with self.subTest(provider=placeholder):
                self.assertTrue(ph in self.app, f"{ph} appears in no layer URL")
                self.assertTrue(f"url.includes('{ph}')" in self.app, f"_resolveTileUrl does not handle {ph}")
                self.assertTrue(f"localStorage.getItem('{storage}')" in self.app, f"{ph} is never read from {storage}")
                self.assertTrue(f"localStorage.setItem('{storage}', key)" in self.app, f"{storage} is never written")
                self.assertTrue(f"u.includes('{ph}')" in self.app, f"the key-missing guard ignores {ph}")
                self.assertTrue(f'id="{input_id}"' in self.html, f"no Settings field for {ph}")
                self.assertTrue(f'onclick="{save_fn}()"' in self.html, f"no Save button calling {save_fn}()")
                self.assertTrue(f"function {save_fn}()" in self.app, f"{save_fn}() is missing")
                # and the field is prefilled from that same storage when Settings opens
                self.assertTrue(f"document.getElementById('{input_id}')" in self.app, f"{input_id} is never prefilled")

    def test_stadia_layers_carry_the_placeholder_in_both_front_ends(self) -> None:
        """Mirror of the CARTO invariant: a stadiamaps tile URL without the placeholder
        is either keyless-by-accident or hardcoded, and both are forbidden."""
        for name, text, expected in (("app.js", self.app, 4), ("lite.js", self.lite, 1)):
            with self.subTest(front_end=name):
                urls = [ln for ln in text.splitlines() if "tiles.stadiamaps.com" in ln]
                self.assertEqual(len(urls), expected, f"{name}: expected {expected} Stadia URLs, saw {len(urls)}")
                missing = [ln.strip()[:70] for ln in urls if "{stadiakey}" not in ln]
                self.assertFalse(missing, f"{name}: stadiamaps URL without {{stadiakey}}: {missing}")

    def test_keyed_layer_labels_carry_the_star_marker(self) -> None:
        """Thunderforest and MapTiler layers were marked with the star in the layer
        menu; CARTO and Stadia layers need a key too, so they carry it as well."""
        for layer in ("voyager", "voyager_nolabels", "positron", "dark_matter", "dark_nolabels",
                      "stamen_toner_lite", "stamen_toner_dark", "stamen_terrain", "stadia_outdoors"):
            with self.subTest(layer=layer):
                line = next(ln for ln in self.app.splitlines() if ln.strip().startswith(layer + ":"))
                self.assertIn("★", line, f"{layer} is missing the star key marker")

    def test_the_thunderforest_hint_names_all_four_layers(self) -> None:
        """It listed only two of the four Thunderforest layers that need the key."""
        # S472: the hint now sits on the key field's own label line
        hint = next(ln for ln in self.html.splitlines() if 'for="tf-api-key-input"' in ln)
        for layer in ("TF Landscape", "TF Outdoors", "TF Pioneer", "TF Atlas"):
            self.assertIn(layer, hint, f"the Thunderforest hint does not mention {layer}")

    def test_the_cached_tile_identity_covers_every_key(self) -> None:
        """_keyTag() makes a cached tile's identity depend on the user's key, so a
        keyed layer must never be tagged ':nokey' for ever. Missed for Stadia when
        the key was added (2026-10-16): the guard, the resolver, the settings field
        and the prefill were all checked, the tag was not."""
        body = self.app[self.app.index("function _keyTag("):]
        body = body[:body.index("\n  }")]
        for placeholder, _storage, _input_id, _save_fn in PROVIDERS:
            with self.subTest(provider=placeholder):
                self.assertIn("{" + placeholder + "}", body, f"_keyTag ignores {{{placeholder}}}")
        self.assertIn("return ''", body, "a layer with no key needs no tag")

    def test_the_default_layer_is_key_free(self) -> None:
        """A keyed default would greet a fresh browser with watermarks and ask for
        a key before the map works at all (and lite has no settings UI to give it
        one). Filip also asked for Esri Topo over the plain OSM default."""
        default = re.search(r"const DEFAULT_TILE_LAYER = '([a-z_]+)'", self.app)
        self.assertIsNotNone(default, "app.js must name the default layer once")
        name = default.group(1)
        self.assertEqual(name, "esri_topo", "the default is Filip's pick")
        line = next(ln for ln in self.app.splitlines() if ln.strip().startswith(name + ":"))
        self.assertNotIn("key}", line, "the default layer must not need an API key")
        # The attribution now links OSM (required); only the TILE URL must not be OSM.
        self.assertNotIn("tile.openstreetmap.org", line.split("attribution:")[0],
                         "he asked for something other than plain OSM")
        # lite has no key fields at all, so its default must be key-free too
        lite_default = re.search(r"const DEFAULT_LAYER = '([a-z_]+)'", self.lite)
        self.assertIsNotNone(lite_default, "lite.js must name its default too")
        lite_line = next(ln for ln in self.lite.splitlines() if ln.strip().startswith(lite_default.group(1) + ":"))
        self.assertNotIn("key}", lite_line, "lite cannot ask for a key, so its default must be key-free")

    def test_the_layer_order_is_free_first_then_grouped_by_provider(self) -> None:
        """Filip, 2026-10-16: "rearrange the order in the layer selection menu —
        free maps first, than API layers, grouped by the provider." """
        block = self.app[self.app.index("const TILE_LAYERS = {"):]
        block = block[:block.index("\n  };")]
        ordered = [m.group(1) for m in re.finditer(r"^    ([a-z_]+):\s*\{", block, re.M)]
        keyed = [k for k in ordered if re.search(r"\{[a-z]+key\}", next(
            ln for ln in block.splitlines() if ln.strip().startswith(k + ":")))]
        self.assertTrue(keyed, "the table still has keyed layers")
        first_keyed = ordered.index(keyed[0])
        self.assertFalse([k for k in ordered[first_keyed:] if k not in keyed],
                         "no free layer may come after a keyed one")
        # each provider forms one contiguous block, in the order the menu shows
        groups, seen = [], []
        for k in ordered:
            line = next(ln for ln in block.splitlines() if ln.strip().startswith(k + ":"))
            ph = next((ph for ph in ("{apikey}", "{mtapikey}", "{cartokey}", "{stadiakey}")
                       if ph in line), "free")
            if not seen or seen[-1] != ph:
                self.assertNotIn(ph, seen, f"{ph} appears in two separate blocks")
                seen.append(ph)
        self.assertEqual(seen, ["free", "{apikey}", "{mtapikey}", "{cartokey}", "{stadiakey}"],
                         f"expected free, Thunderforest, MapTiler, CARTO, Stadia — saw {seen}")

    def test_the_menu_says_when_a_keyed_layer_has_no_key(self) -> None:
        self.assertTrue("if (_layerKeyMissing(key)) {" in self.app,
                        "the layer menu must mark layers that still need a key")
        self.assertTrue("key needed" in self.app, "and say so in words, not only with the star")


if __name__ == "__main__":
    unittest.main()
