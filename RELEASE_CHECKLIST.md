# OverMesh Release Checklist

This checklist tracked the preparation of OverMesh for public release. The push is done as of 2026-04-13.

## Verified on the live app

As of 2026-04-11, the live app on `http://127.0.0.1:8082` is up and responding.

Meshtastic radio status is live, with `EDC-1` connected.

MeshCore radio status is live, with `MHQ-1` connected.

MT channels are readable through the API, currently `LongFast`, `Slovenija`, and `Don't Panic`.

MC channels are readable through the API, including `Public`, `#slovenija`, and `Don't Panic`.

Cross system rules are persisted in config and readable through the API.

Saved cross rules currently include:

1. `Bridge`, MT `Don't Panic` and MC `Don't Panic`
2. `Mirror`, MT `LongFast` to MC `Don't Panic`
3. `Mirror`, MC `Public` to MT `Don't Panic`

Live cross forwarding was tested during development and confirmed working in both directions.

The app README has been rewritten to describe this repo as the main active OverMesh app, with MT and MC as equal parts of one tool.

Manual checks confirmed on 2026-04-11:

1. MT chat works
2. MC chat works
3. Map works
4. Sense works
5. Bot works
6. Cross system forwarding works
7. Notifications work
8. MC channel history deletion clears normal chat messages on `Don't Panic`
9. MT channel history deletion clears `Don't Panic` completely
10. Restart works
11. Notifications still work after restart

## Still needs a manual release pass

User confirmed on 2026-04-12 that the browser/features sanity pass was already done on 2026-04-11 during live use.

Use this section only for any re-checks needed after further code changes:

1. Fresh startup from a clean app launch, then first load in the browser
2. Add or reconnect an MT radio from Settings
3. Add or reconnect an MC radio from Settings
4. Check MT and MC DM flows
5. Open Map, Nodes, Chat, Sense, and Settings and confirm they all render cleanly after reload
6. Re-test MC history deletion once after the full wipe change, to confirm bot/system-style channel entries are removed too

## Must have before push as the new public OverMesh

1. Keep the README aligned with the actual repo name and release plan
2. DONE, add or refresh screenshots so they match the current app, not older MT only views
3. DONE, do one clean manual sanity pass and record any failures
4. DONE, reviewed app naming — stale `overmesh-mc` references cleaned up, launch scripts renamed, log path fixed
5. DONE, public release text is `RELEASE_NOTES.md`
6. DONE, improve cross-platform install clarity in the public docs:
   - keep Linux as the strongest path
   - add clear native install guidance framing for Windows and macOS too
7. DONE, remove avoidable Linux-only wording from public-facing UI/help text where it affects all users
8. DONE, replace Linux-only first-run config assumptions with a more platform-neutral starter example
9. DONE, keep the release focused on broad native usability and simple setup, not Docker or other advanced deployment paths

## Should have soon after

1. A clearer migration note for people coming from the older OverMesh repo
2. A small known limits section in the public release text
3. A follow up pass on installation and service instructions on a second machine
4. DONE, make restart behavior platform-aware instead of assuming Linux shell tools and `/tmp`
5. DONE, Linux convenience scripts (`launch-overmesh.sh`, `launch-direct.sh`) clearly separated from default install
6. DONE, helper scripts renamed and hard-coded local paths fixed (`/tmp/overmesh-mc.log` → `/tmp/overmesh.log`)

## Can wait

1. DM bridging
2. More advanced cross system rule logic
3. Broader polish around edge case MC metadata
4. Docker support as an optional advanced deployment path
