# assets/sfx

Optional slide-transition sound effects. Name files `sfx_<index>.mp3` /
`sfx_<index>.wav` to bind each clip to a specific slide (0-based), or drop any
loose files here and they are cycled in slide order.

Mechanism (R8-B4): each clip is delayed to its slide's start time and mixed
into the final audio under the narration at fixed gain (`0.9`). Prefer short,
quiet whooshes/ticks — the voiceover always stays on top.

Same licensing rules as `assets/music`: only use clips you have the right to
use, and note credits in `CREDITS.txt`.