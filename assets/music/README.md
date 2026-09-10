# assets/music

Place music tracks here (`.mp3`/`.wav`/`.m4a`/`.ogg`). Any file present stops
the assembly from running mute: the music bed is ducked under the voiceover
with a sidechain compressor (`MUSIC_DUCK_*` in `automato/config.py`).

## Licensing

Only use tracks you have the right to use. Good sources for monetization-safe
music:

- **CC0** — no attribution required (e.g. Pixabay Music, Free Music Archive's
  CC0 filter, Uppbeat free tier is CC-licensed).
- **CC-BY** — attribution required (State your credit in the video description
  via the channel's auto-built metadata).
- YouTube's own **Audio Library** (free, monetization-safe on YouTube).

Keep a `CREDITS.txt` here naming each file and its license/author. The engine
does not validate licenses — that responsibility stays with the operator.