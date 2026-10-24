"""Settings layout refresh (S472): the page was rebuilt into cards and collapsible groups.

These guard what the rebuild had to keep: every element the old Settings page had (JS reads them
all by id, so a missing one fails silently at runtime), one remembered key per collapsible group,
and no code that finds a heading by its position in the markup (the old Identity heading lookup
grabbed "the first h3", which the regrouping would have broken).
"""
import re
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

# Every id inside the Settings panel before the S472 refresh (frozen on purpose: removing a
# control must be a deliberate change to this list, not an accident of a markup edit).
SETTINGS_IDS_BEFORE_S472 = (
    'add-serial-fields', 'add-tcp-fields', 'add-type-serial', 'add-type-tcp', 'alert-log-geofence',
    'alert-log-messages', 'alert-log-node-new', 'alert-log-node-return', 'alert-log-radio', 'alert-log-rc-collect',
    'alert-log-rc-new-node', 'auth-enabled-toggle', 'auth-password', 'auth-settings-status', 'auth-username',
    'bridge-ingest-enabled', 'bridge-ingest-token', 'bridge-settings-status', 'bridge-webhook-secret',
    'bridge-webhook-urls', 'bridge-webhooks-enabled', 'btn-ch-key-eye', 'btn-ch-remove', 'btn-from-gps',
    'carto-api-key-input', 'carto-key-status', 'ext-map-select', 'gps-auto-push-mc-toggle',
    'gps-auto-push-mt-toggle', 'gps-card-alt', 'gps-card-fix', 'gps-card-lat', 'gps-card-lon', 'gps-card-sats',
    'gps-card-speed', 'gps-clear-status', 'gps-live-card', 'gps-port-label', 'gps-port-row', 'gps-port-select',
    'gps-precision-label', 'gps-precision-slider', 'gps-push-interval-unit', 'gps-push-interval-val',
    'gps-push-status', 'gps-share-block', 'gps-share-manual-note', 'gps-source-note', 'gps-source-select',
    'gps-status', 'inapp-notif-messages', 'inapp-notif-nodes', 'inapp-notif-returned',
    'inapp-notif-returned-gap-unit', 'inapp-notif-returned-gap-value', 'map-layer-color', 'map-layer-file',
    'map-layer-geojson', 'map-layer-list', 'map-layer-name', 'map-layer-status', 'mc-actions-status',
    'mc-add-ble-fields', 'mc-add-serial-fields', 'mc-add-tcp-fields', 'mc-add-type-ble', 'mc-add-type-serial',
    'mc-add-type-tcp', 'mc-auto-add-contacts', 'mc-auto-add-status', 'mc-ch-edit-title', 'mc-ch-key-hex',
    'mc-ch-key-type', 'mc-ch-name', 'mc-channel-edit', 'mc-channels-list', 'mc-channels-remove-status',
    'mc-channels-section', 'mc-channels-status', 'mc-coords-lat', 'mc-coords-lon', 'mc-coords-prec-val',
    'mc-coords-precision', 'mc-debug-btn', 'mc-device-info-content', 'mc-device-name-input', 'mc-force-flood',
    'mc-force-flood-status', 'mc-import-ch-name-preview', 'mc-import-ch-secret-preview', 'mc-import-ch-slot',
    'mc-import-channel-link', 'mc-import-channel-preview', 'mc-import-channel-section', 'mc-import-channel-status',
    'mc-import-contact-section', 'mc-import-link', 'mc-import-status', 'mc-loc-policy-btn',
    'mc-node-settings-section', 'mc-passive-collection', 'mc-passive-collection-status',
    'mc-passive-intel-section', 'mc-path-hash-mode', 'mc-path-hash-status', 'mc-radio-bw', 'mc-radio-cr',
    'mc-radio-freq', 'mc-radio-sf', 'mc-radio-status', 'mc-reset-all-paths-status', 'mc-scope-default-input',
    'mc-scope-default-label', 'mc-scope-discover-btn', 'mc-scope-discover-chips', 'mc-scope-discover-results',
    'mc-scope-discover-sources', 'mc-scope-discover-status', 'mc-scope-fw-note', 'mc-scope-per-channel',
    'mc-scope-region-add', 'mc-scope-region-chips', 'mc-scope-region-presets', 'mc-scope-status',
    'mc-settings-radio-select', 'mc-stats-content', 'mc-tx-power', 'mc-tx-power-max', 'mt-api-key-input',
    'mt-cleanup-days', 'mt-cleanup-enabled', 'mt-cleanup-status', 'mt-import-ch-key-preview',
    'mt-import-ch-name-preview', 'mt-import-ch-slot', 'mt-import-channel-link', 'mt-import-channel-preview',
    'mt-import-channel-status', 'mt-key-status', 'node-cfg-actions-status', 'node-cfg-bluetooth-status',
    'node-cfg-bt-enabled', 'node-cfg-bt-mode', 'node-cfg-bt-pin', 'node-cfg-bt-pin-wrap', 'node-cfg-ch-downlink',
    'node-cfg-ch-edit-title', 'node-cfg-ch-key-display', 'node-cfg-ch-key-row', 'node-cfg-ch-name',
    'node-cfg-ch-psk-hex', 'node-cfg-ch-psk-type', 'node-cfg-ch-role', 'node-cfg-ch-uplink',
    'node-cfg-channel-edit', 'node-cfg-channels-list', 'node-cfg-channels-remove-status',
    'node-cfg-channels-status', 'node-cfg-content', 'node-cfg-device-status', 'node-cfg-display-status',
    'node-cfg-display-units', 'node-cfg-error', 'node-cfg-fixed-alt', 'node-cfg-fixed-coords',
    'node-cfg-fixed-lat', 'node-cfg-fixed-lon', 'node-cfg-fixed-pos', 'node-cfg-flip-screen', 'node-cfg-gps-mode',
    'node-cfg-hoplimit', 'node-cfg-ignore-mqtt', 'node-cfg-led-heartbeat-disabled', 'node-cfg-long-name',
    'node-cfg-lora-status', 'node-cfg-mqtt-address', 'node-cfg-mqtt-enabled', 'node-cfg-mqtt-encryption',
    'node-cfg-mqtt-json', 'node-cfg-mqtt-map', 'node-cfg-mqtt-map-interval', 'node-cfg-mqtt-map-location',
    'node-cfg-mqtt-map-precision', 'node-cfg-mqtt-password', 'node-cfg-mqtt-root', 'node-cfg-mqtt-status',
    'node-cfg-mqtt-tls', 'node-cfg-mqtt-username', 'node-cfg-network-status', 'node-cfg-ok-to-mqtt',
    'node-cfg-owner-status', 'node-cfg-pos-broadcast', 'node-cfg-pos-prec-val', 'node-cfg-pos-precision',
    'node-cfg-position-status', 'node-cfg-power-saving', 'node-cfg-power-status', 'node-cfg-preset',
    'node-cfg-region', 'node-cfg-role', 'node-cfg-screen-secs', 'node-cfg-short-name', 'node-cfg-shutdown-secs',
    'node-cfg-smart-pos', 'node-cfg-tabs', 'node-cfg-tel-device', 'node-cfg-tel-env', 'node-cfg-telemetry-status',
    'node-cfg-txpower', 'node-cfg-wifi-ap-mode', 'node-cfg-wifi-enabled', 'node-cfg-wifi-psk',
    'node-cfg-wifi-psk-eye', 'node-cfg-wifi-ssid', 'notif-messages', 'notif-nodes', 'om-pos-lat', 'om-pos-lon',
    'om-pos-manual-row', 'om-pos-status', 'panel-settings', 'passive-collector-key', 'passive-collector-label',
    'passive-collector-search', 'passive-collector-search-results', 'passive-collector-status',
    'passive-collectors-list', 'passive-intel-list', 'passive-intel-stats', 'passive-ttl-days',
    'rc-auto-collect-interval', 'rc-auto-collect-status', 'region-config', 'region-config-estimate',
    'region-config-title', 'region-dl-bar', 'region-dl-btn', 'region-dl-layer', 'region-dl-progress',
    'region-dl-text', 'region-list', 'region-max-z', 'region-min-z', 'region-search', 'region-search-results',
    'settings-accent-swatch', 'settings-add-error', 'settings-date-format', 'settings-distance-unit',
    'settings-mc-add-error', 'settings-mc-bt-address', 'settings-mc-bt-pin', 'settings-mc-node-name',
    'settings-mc-nodes-list', 'settings-mc-port-select', 'settings-mc-tcp-host', 'settings-mc-tcp-port',
    'settings-node-cleanup-btn', 'settings-node-cleanup-days', 'settings-node-name', 'settings-nodes-list',
    'settings-port-select', 'settings-sense-cooldown', 'settings-sense-cooldown-display', 'settings-tcp-host',
    'settings-tcp-port', 'settings-time-format', 'settings-update-check-btn', 'settings-update-log',
    'settings-update-restart-btn', 'settings-update-run-btn', 'settings-update-summary', 'settings-zoom-display',
    'settings-zoom-slider', 'shared-tile-server-status', 'shared-tile-server-toggle', 'shared-tile-server-url',
    'sound-notif-messages', 'sound-notif-nodes', 'sound-notif-radios', 'st-app', 'st-mc', 'st-mt', 'st-mt-import',
    'stab-app', 'stab-mc', 'stab-mt', 'stadia-api-key-input', 'stadia-key-status', 'tf-api-key-input',
    'tf-key-status', 'tile-autocache-toggle', 'tile-cache-count', 'tile-estimate', 'tile-extra-zoom',
    'tile-progress-bar', 'tile-progress-text', 'tile-progress-wrap', 'tile-save-btn', 'tile-save-layer-label',
)


def _settings_panel(html: str) -> str:
    start = html.index('<div class="panel" id="panel-settings">')
    return html[start:html.index("<!-- Pip map", start)]


class SettingsLayoutTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.html = (REPO / "templates" / "index.html").read_text(encoding="utf-8")
        cls.panel = _settings_panel(cls.html)
        cls.app = (REPO / "static" / "js" / "app.js").read_text(encoding="utf-8")

    def test_every_pre_refresh_settings_id_still_exists(self) -> None:
        ids = set(re.findall(r'\bid="([^"]+)"', self.panel))
        missing = [i for i in SETTINGS_IDS_BEFORE_S472 if i not in ids]
        self.assertEqual(missing, [], f"Settings lost element(s) the JS reads by id: {missing}")

    def test_ids_in_the_panel_are_unique(self) -> None:
        ids = re.findall(r'\bid="([^"]+)"', self.panel)
        dupes = sorted({i for i in ids if ids.count(i) > 1})
        self.assertEqual(dupes, [], f"duplicate ids in Settings: {dupes}")

    def test_every_group_has_a_unique_key(self) -> None:
        groups = re.findall(r'<details class="st-group"([^>]*)>', self.panel)
        self.assertGreater(len(groups), 10, "expected the collapsible groups of the S472 layout")
        keys = []
        for attrs in groups:
            m = re.search(r'data-key="([^"]+)"', attrs)
            self.assertIsNotNone(m, f"a group has no data-key, its open state cannot be remembered: {attrs}")
            keys.append(m.group(1))
        self.assertEqual(len(keys), len(set(keys)), f"two groups share a key: {keys}")

    def test_no_heading_is_found_by_position(self) -> None:
        self.assertNotIn("querySelector('#node-cfg-content h3')", self.app,
                         "the hardware label must be written by id (node-cfg-hw), not into 'the first h3'")
        self.assertIn('id="node-cfg-hw"', self.panel)

    def test_settings_checkboxes_are_switches(self) -> None:
        """Native checkboxes in Settings are drawn as switches; each must carry the class (or sit in a
        .bot-toggle), otherwise one plain checkbox appears among the switches."""
        plain = []
        for m in re.finditer(r'<input[^>]*type="checkbox"[^>]*>', self.panel):
            tag = m.group(0)
            if "st-switch" in tag or " hidden" in tag:
                continue
            before = self.panel[max(0, m.start() - 60):m.start()]
            if 'class="bot-toggle"' in before:
                continue
            plain.append(tag)
        self.assertEqual(plain, [], f"checkbox(es) without the switch style: {plain}")


if __name__ == "__main__":
    unittest.main()
