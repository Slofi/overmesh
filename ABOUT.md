# About OverMesh

I build things. Meshtastic and MeshCore networks, portable computers, antennas, mostly for the satisfaction of making something that actually works, not just something that looks cool and sort of works..

I'd been putting together a cyberdeck: a portable, self-contained field computer built around a Rock 5B single-board computer. Meshtastic was already part of it: several DIY nodes, a small local mesh, the whole thing. The cyberdeck needed a dashboard, something to show the network at a glance: who's online, signal quality, active channels, maybe send a message.

The existing options were fine, just not quite right for me. So I built one.

OverMesh started as a small Flask app, just enough to show a node list and send a chat message. Over the following weeks it grew into a bit more than that: a live map with offline tiles, several radios at once, a bot, Mesh Sense (a passive listening overlay), Marks (waypoints shared over the mesh) and node settings straight from the browser. Later MeshCore came in too, and these days it's the stronger side of the app, if you ask me. Each piece was added when I actually needed it, which I think is the right way to build something.

I have no programming background, and I wasn't going to learn Python fast enough to build this in three weeks. The code was written with AI help: Claude (Anthropic's coding assistant), and later also DeepSeek. The ideas and the requirements were mine, and so were the "this is broken" and "that's not what I meant" moments. I tested everything on real hardware against a real mesh and sent it back until it worked the way it needed to.

It's a tool I use every day. It runs on my cyberdeck, my laptop and my home server, and it does what I need it to do.

Maybe it'll do what you need too.
