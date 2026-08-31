# Radial Rhythm Game — Pyglet

Beats fly from the **outside of the screen into the centre** along 4 coloured lanes.
Your music is turned into a beatmap **synced to the actual voice/melody** (via `madmom`),
not a metronome, so the notes follow what you sing/what you hear.

| Lane | Key | Colour | Direction |
|------|-----|--------|-----------|
| D | D | Red `#FF4A4A` | Left (180°) |
| F | F | Blue `#4A90FF` | Top (90°) |
| J | J | Green `#4AFF8A` | Right (0°) |
| K | K | Yellow `#FFD74A` | Bottom (270°) |

Hit the key when the beat reaches the centre ring.

## Features
- **Main Menu** → `PLAY` / `OPEN FILE` / `SETTINGS` / `QUIT` (osu!-style big-play-button layout).
- **osu!-style song select**: vertical carousel with the selected card "pulled out",
  a **live video preview** (full-bleed background, ≥30 fps) and a **song-colour accent**
  (the selected card + difficulty tab are tinted with the song's average colour, extracted
  with `ffmpeg` and cached).
- **3 difficulties**, each voice-first at a different density:
  - `EASY` — melody & voice • ~1.5 notes/s • low density
  - `MEDIUM` — melody + snare/bass • ~2.1 notes/s • moderate density
  - `HARD` — full groove • ~3.0 notes/s • high density
- **1–20 rating** per song/difficulty (density → rating). Click or use `1/2/3` / `←/→`
  to pick a difficulty; `ENTER` plays.
- **Auto beatmap from any mp4/mp3/wav/m4a/ogg/flac/mov/mkv**:
  1. extracts mono WAV with `ffmpeg`
  2. runs `madmom` `RNNOnsetProcessor` onset detection
  3. weights onsets toward the **voice/melody** (harmonic vs percussive flux),
     then maps beats to the 4 lanes
- **Caching**: each song+difficulty analysis is cached to `songs/.cache/*.json`
  (versioned), so re-loading is instant.

## Install

Requires Python 3.10+ and `ffmpeg` on `PATH`.

```powershell
python -m pip install --upgrade pip setuptools "setuptools<82" pyglet numpy scipy
# (Python 3.14 users: use --no-build-isolation for madmom)
python -m pip install --no-build-isolation madmom==0.16.1
```

`ffmpeg` is already bundled under `ffmpeg_shared/` (auto-prepended to `PATH` and
`PYGLET_FFMPEG_LOCATION`). Verify with `ffmpeg -version`.

## Run
```powershell
python main.py
# or with a file directly:
python main.py "C:\path\to\song.mp4"
# putting files in songs/ is preferred:
#   songs/my_song.mp4 -> appears in the PLAY song select
```

`_example_beats.wav` is included in `songs/` so the browser is never empty.

## Controls
### Menu
- Menu: `PLAY` / `OPEN FILE` / `SETTINGS` / `QUIT`.
- `UP/DOWN` or `W/S` navigate, `ENTER/SPACE` select, `ESC` quit, `O` open external file.

### Settings
- `UP/DOWN` navigate settings, `ENTER` or `LEFT/RIGHT` toggle a setting, `B`/`ESC` back.
- **Fullscreen** (ON/OFF) — persisted to `config.json` next to `main.py`, so it's remembered on
  next launch. `F11` toggles fullscreen at any time (and persists it too).

### Song select (osu!-style)
- `UP/DOWN` or `W/S` browse songs (clicking a card also selects it).
- `LEFT/RIGHT` or `1/2/3` pick difficulty (click a difficulty button too).
- `ENTER` — choose difficulty & play (analyses on first load, then cached).
- `R` refresh the folder scan, `B`/`ESC` back to menu.

### Difficulty select
- `UP/DOWN`/`LEFT/RIGHT` or `1/2/3` pick difficulty, `ENTER` play, `B`/`ESC` back.

### Playing
- `D` `F` `J` `K` — hit (lowercase also works)
- `SPACE` pause/resume, `ESC` → menu (stops music)
- `+`/`-` or `[`/`]` adjust sensitivity (re-open song to apply) before loading

Scoring: `PERFECT ±130ms` 300, `GOOD ±260ms` 150, `OK ±350ms` 50, else `MISS`.
Combo multiplier caps at 2.0×.

## How a beatmap is made (`main.py:detect_beats_madmom`)
1. `extract_wav_with_ffmpeg()` → high-quality mono WAV.
2. `read_wav_mono()` → float32 `[-1, 1]`.
3. `madmom` `RNNOnsetProcessor` → per-frame onset activation (100 fps).
4. Harmonic/percussive separation (median-filtered STFT) produces a per-frame **voice
   weight**; onsets near the voice/melody are prioritised → `actions`.
5. Onset peaks are picked to the chosen difficulty's density target, then timed to the
   `spectral-flux`-estimated BPM grid and mapped to lanes.
6. Result cached by `{song}_{hash}_{difficulty}.json` (`CACHE_VERSION` gated).

## File layout
```
rhythmgame/
  main.py            # full game (window, states, rendering, input, analysis)
  songs/             # put mp4/mp3/wav etc here
    .cache/          # cached beatmaps + song colour accents
    _example_beats.wav
  ffmpeg_shared/     # bundled FFmpeg (win64 shared build)
  requirements.txt
  README.md
```

## Troubleshooting
- *No beats detected* → increase sensitivity (`+`) and reopen, or pick an audio track
  with a clear voice/percussion.
- *Video shows black in preview* → audio still syncs; video texture playback is
  incidental, the timer/beatmap is authoritative.
- *Song colour tab looks dull* → the accent is the frame average; very dark songs stay
  dark by design (a min-brightness floor is enforced).
- *Songs not appearing* → check `songs/` exists next to `main.py`, use a supported
  extension, and press `R` in song select.
