# Radial Rhythm Game — Pyglet

Beats fly from the **outside of the screen into the centre**. 4 colours, 4 keys:

| Lane | Key | Colour | Direction |
|------|-----|--------|-----------|
| D | D | Red `#FF4A4A` | Left (180°) |
| F | F | Blue `#4A90FF` | Top (90°) |
| J | J | Green `#4AFF8A` | Right (0°) |
| K | K | Yellow `#FFD74A` | Bottom (270°) |

Hit the key when the beat reaches the centre ring.

## Features
- **Main Menu** → `PLAY DEMO` / `SONGS` / `QUIT`
- **Songs folder** `./songs/` — drop mp4/mp3/wav/m4a/ogg/flac/mov/mkv there and pick from `SONGS` menu. Scans on entry, `R` to refresh.
- **Demo pattern** when no song — 16-bar pre-planned chart at 128 BPM to test mechanics.
- **MP4 sync** — give it an `mp4` (or any supported audio/video) and it auto-generates a beatmap:
  1. Extracts mono WAV with `ffmpeg`
  2. Runs lightweight onset detection with `numpy` (adaptive energy + peak picking).  
     If `librosa` is installed, it uses `librosa.beat.beat_track` for higher accuracy.
  3. Assigns beats to 4 lanes and plays back in sync with `pyglet.media.Player` (`player.time` is the clock).

## Install
```powershell
pip install pyglet numpy
# optional - better sync:
pip install librosa
# ffmpeg must be on PATH (already installed on this machine via winget)
ffmpeg -version
```

## Run
```powershell
python main.py
# or with a file directly:
python main.py "C:\path\to\song.mp4"
# putting files in songs/ is preferred:
#   songs/my_song.mp4 -> appears in SONGS menu
```

`_example_beats.wav` is included in `songs/` as a test tone so the menu isn't empty.

## Controls
### Menu
- `UP/DOWN` or `W/S` navigate, `ENTER/SPACE` select, `ESC` quit
- `O` open external file from anywhere

### Songs browser
- `UP/DOWN` or `W/S` select track, `ENTER/SPACE` play (analyses then starts)
- `R` refresh folder scan, `B`/`ESC` back to menu, `O` open external

### Playing
- `D` `F` `J` `K` — hit (lowercase also works)
- `SPACE` pause/resume, `ESC` → menu (stops music)
- `+`/`-` or `[`/`]` adjust sensitivity (re-open song to apply) before loading

Scoring: `PERFECT ±130ms` 300, `GOOD ±260ms` 150, `OK ±350ms` 50, else `MISS`. Combo multiplier `+25%` per 8 combo.

## How sync works (`main.py:detect_beats_energy`)

1. `extract_wav_with_ffmpeg()` -> 44.1k mono s16le
2. `read_wav_mono()` -> float32 `[-1,1]`
3. `detect_beats_energy()` -> RMS envelope per 512 hop, adaptive threshold `local_mean * 1.55 + local_std*0.35`, peak-pick with `180ms` min distance.
4. Times mapped to lanes `LANE_ORDER[idx%4]` cycling (with chord doubling every 16th beat) so all 4 colours appear.
5. Fallback: if <8 onsets, generate 120 BPM grid.

Tweak `sensitivity` (0.6–2.0) to shift threshold lower/higher.

## File layout
```
rhythmgame/
  main.py          # full game (window, states, rendering, input, analysis)
  songs/           # put mp4/mp3/wav etc here
    _example_beats.wav
    _put_songs_here.txt
  requirements.txt
  README.md
```

## Troubleshooting
- *Label bold error* (pyglet 2.1) — fixed: uses `weight='bold'` not `bold=True`.
- *No beats detected* -> increase sensitivity (`+`) and reopen file, or `pip install librosa`.
- *Video shows black* -> audio still syncs; video texture display is not required for gameplay (progress bar + timer is authoritative).
- *No tkinter dialog* -> falls back to console `input()` path prompt.
- *Songs not appearing* -> check `songs/` exists next to `main.py`, use supported extensions, press `R` in Songs screen.

## Fix log
- Fixed `TypeError: Label.__init__() got an unexpected keyword argument 'bold'` and `_boxes` deallocator crash by switching to `weight='bold'`.
- Added state machine `menu` / `song_select` / `playing` / `paused` / `results` and folder browser.
