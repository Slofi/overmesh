# S485 handover (EST-320: minimap)

Branch `s485-report`, commit `a2500fc` (base `cc948e6`). Gate: `tests/run.sh` exit 0,
including eslint. Not pushed, not deployed, VERSION untouched.

## Minimap: my position / CD / node location is not shown

FIXED.

What was wrong: the minimap built its own marker lists. It drew MT nodes and MC
contacts, but never the connected MC radio's own position, and it never drew the
OM Position at all. On the CD with an MC radio that left the minimap with no
marker for us.

What it does now: one shared resolver, `_omOwnPositions()`, returns the OM
Position (hand-picked, or a live GPS fix), each connected MC radio's own position
and the MT local node. The minimap draws that list with the same marker builders
the main map uses, so the MC radio keeps its green diamond and our own point its
blue you-dot. A source with no position adds no marker: no fallback to an old
point, no guess. The Locate button and the first auto-centre use the same list,
so Locate works on MC too.

How Filip can see it: open the minimap on Nodes or Chat. Your own point is on it
on MC, on MT and with a hand-picked OM Position. If there is no real position,
there is no marker.

What I could not test: real radios and a real browser. The resolver and the
marker wiring are covered by `tests/test_pip_own_position.js`, which fails on
`cc948e6` because the resolver did not exist.

## Minimap on CD: cannot resize with a finger

FIXED.

What was wrong: the top-left grip listened to `mousedown` only, so a touch never
reached it, and the grip was 18x18, too small for a finger.

What it does now: the grip uses pointer events with pointer capture, so finger
and mouse take the same path. `pointerdown` calls preventDefault and
stopPropagation, and the CSS sets `touch-action: none` on the grip, so dragging
it does not pan the page or the map. The grip is 44x44. The visible corner mark
stays small. The header mouse drag is unchanged.

How Filip can see it: open the minimap on the CD touchscreen and drag the
top-left corner with a finger. It should resize, and the page behind should stay
put. A mouse still works the same way.

What I could not test: a real touchscreen. The drag maths, pointer capture,
clamping and the CSS size are covered by `tests/test_pip_resize_touch.js`, which
fails on `cc948e6` because the grip has no pointerdown listener. The one thing
left for a real check is the finger feel on the CD itself.
