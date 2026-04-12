# Moving from older OverMesh

If you used the older OverMesh before the MT plus MC merge, this is the continuation of that app, not a separate project.

The biggest change is that Meshtastic and MeshCore now live together in one interface. You can still use OverMesh with only one system if that is all you have, but the app is no longer built around MT first with MC added later.

## What stays familiar

The core idea is still the same: local dashboard, your radios, your machine, no cloud, no account, no subscription.

Map, Nodes, Chat, Sense, bot tools, radio settings, and the general layout should still feel familiar.

## What changed

MeshCore is now a full part of the app, with its own radios, channels, contacts, chat, bot support, and map and Nodes integration.

There are also cross-system tools now, so MT and MC traffic can be mirrored or bridged between selected channels when needed.

Settings and saved config now include both MT and MC radios, along with newer app features like in-app notifications and cross-system rules.

## What to check when moving over

1. Review `config.json`, especially radio entries, host and port, and any older assumptions about a Meshtastic-only setup
2. Recheck your service file if you had a custom user service before
3. Open `Settings` and confirm your MT and MC radios are present and connected
4. Test chat, map, and bot behavior once before treating the new app as your normal daily build

## Current limits

Cross system forwarding is channel to channel right now.

DM bridging is not implemented yet.

Some MeshCore metadata is still best effort, especially where MT and MC expose different radio details.
