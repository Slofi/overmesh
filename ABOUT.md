# About OverMesh

I build things. Meshtastic and Meshcore networks, portable computers, antennas; mostly for the satisfaction of making something that actually works, not just something that looks cool and sort of works..

I'd been putting together a Cyberdeck — a portable, self-contained field computer built around a Rock 5B single-board computer. Meshtastic was already part of the setup: several DIY nodes, a small local mesh, the whole thing. The Cyberdeck needed a dashboard. Something to show the network at a glance: who's online, signal quality, active channels, maybe send a message.

The existing options were fine. Just not quite right. So I built one.

OverMesh started as a small Flask app, just enough to show a node list and push a chat message. What it grew into over the following weeks is... a bit more than that. A live map with offline tile caching, multi-radio support, a bot, Mesh Sense (passive listening overlay), Marks (waypoint exchange over the mesh), node configuration directly from the browser. It grew piece by piece, each feature added when it turned out to be needed — which is, I think, the right way to build something.

It was developed with the help of Claude (Anthropic's AI coding assistant). I'll be honest about that. I have no programming background; the code wasn't going to write itself, and I wasn't going to learn Python fast enough to build this in three weeks. But I directed every decision, tested everything on real hardware against a real mesh, caught a lot of bugs, and pushed back when something didn't work the way it needed to. The ideas, the requirements, the "this is broken" and "that's not what I meant" — that was all me. Claude handled the implementation. I think that's a fair description of how it actually went.

The result is a tool I use every day. It runs on the Cyberdeck and on my laptop. It does what I need it to do.

Maybe it'll do what you need too.
