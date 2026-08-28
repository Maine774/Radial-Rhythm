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
- **Demo pattern** when no song is provided — 16-bar pre-planned chart at 128 BPM to test mechanics.
- **MP4 sync** — give it an `mp4` (or mp3/wav/mov/mkv) and it auto-generates a beatmap:
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
```

## Controls
- `O` — open file dialog (mp4 / video / audio)
- `SPACE` — play demo (no file) or play loaded media. Press again to pause.
- `P` — play loaded media (alternative)
- `D` `F` `J` `K` — hit (lowercase also works, key-repeat friendly)
- `+` / `-` (or `[` `]`) — adjust sensitivity (more beats vs fewer) before re-loading file
- `ESC` — quit

Scoring: `PERFECT ±130ms` 300, `GOOD ±260ms` 150, `OK ±350ms` 50, else `MISS`. Combo multiplier `+25%` per 8 combo.

## How sync works (`main.py:detect_beats_energy`)

1. `extract_wav_with_ffmpeg()` -> 44.1k mono s16le
2. `read_wav_mono()` -> float32 `[-1,1]`
3. `detect_beats_energy()` -> RMS envelope per 512 hop, adaptive threshold `local_mean * 1.55 + local_std*0.35`, peak-pick with `180ms` min distance.
4. Times mapped to lanes `LANE_ORDER[(idx*7 + int(t*2))%4]` with chord doubling every 16th beat.
5. Fallback: if <8 onsets, generate 120 BPM grid.

Tweak `sensitivity` (0.6–2.0) to shift threshold lower/higher.

## File layout
```
rhythmgame/
  main.py          # full game (window, rendering, input, analysis)
  requirements.txt
  README.md
```

## Troubleshooting
- *No beats detected* -> increase sensitivity (`+` key) and reopen file, or install `librosa`.
- *Video shows black* -> audio still syncs; video texture display is not required for gameplay (progress bar + timer is authoritative).
- *No tkinter dialog* -> falls back to console `input()` path prompt.
