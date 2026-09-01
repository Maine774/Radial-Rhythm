# Radial Rhythm — Game Plan

> Codebase: `C:\Users\LOK0008\rhythmgame\main.py` (~3000 lines, `pyglet 2.1.16`, `numpy 2.5.2`, `madmom 0.16.1`, `librosa 1.0.0`)
> State machine: `self.state` (`main.py`) — `menu | song_select | difficulty_select | analyzing | playing | paused | results | settings | keybinds`

---

## 1. Initial Game Idea

**Genre:** Radial / music-synced tap rhythm game I'm building in Pyglet. Notes spawn outside
the ring and **spiral inward** to a central target. I hit the matching lane key at the moment the
beat reaches the centre.

**Elevator Pitch:** Drop an `mp4 / mp3 / wav / m4a / ogg / flac` into `songs/`
(`SONGS_DIR`, `SUPPORTED_EXTS`), and the game auto-generates a beatmap **synced to the actual
voice/melody** — not a metronome — using `ffmpeg` + `madmom` `RNNOnsetProcessor` with
harmonic/percussive weighting. The video background (if any) stays audio-synced via `pyglet`
`FFmpeg` (bundled `ffmpeg_shared/.../bin`). I have three difficulties sharing the same
voice-first logic at different densities.

**Core Loop:** `Select Song → Choose Difficulty → Analyse (cached next time) → Play → Hit / Miss → Score + Combo → Results → Play Again / Quit`

### 1.1 Radial Mechanic

- Window `1280×720` (`WINDOW_W,WINDOW_H`), centre `640,360` (`CENTER`), `TARGET_RADIUS=70`,
  `SPAWN_RADIUS=520`, `TRAVEL_TIME=1.6s`
- **Spiral approach (Day 2):** each note starts on the predecessor lane's side and winds 90°
  inward to its own side. `spiral_point(hit_ang, cw, raw)`:
  `radius = SPAWN − raw*(SPAWN−TARGET)`, `angle = start ∓ raw*90`, where
  `start = hit+90` (clockwise) or `hit−90` (counterclockwise, new-section telegraph).
- 4 Lanes (`LANES`, `LANE_ORDER`):
  | Lane | Key | Colour | Ends at |
  |------|-----|--------|---------|
  | `d` | `D` | `255,74,74` red | 180° left |
  | `f` | `F` | `74,144,255` blue | 90° top |
  | `j` | `J` | `74,255,138` green | 0° right |
  | `k` | `K` | `255,215,74` yellow | 270° bottom |
- The chain loop is `yellow → red → blue → green → yellow` (clockwise). A gap > 2.0s to the
  previous note marks a new section and flips that note to counterclockwise + a ghost arc.

### 1.2 Scoring

`HIT_WINDOW_PERFECT=0.13s → 300pts`, `GOOD 0.26s → 200`, `MEH 0.35s → 100`, else `MISS` (0).
`try_hit()` picks the closest `active_beats` within `MEH` and always plays the keypress SFX
(`SFX/clickfx.mp3`, volume = `fx_volume`). Combo `combo++` per hit, `max_combo`
tracked, multiplier `1 + min(combo//8,4)*0.25` max `2.0×` at 32. `MISS` (timeout `>0.35` in
`update` or wrong lane) resets `combo=0`. **Perfect combo (FC)** `fc++` on perfect only;
any other result calls `_break_fc()` (`fc=0`), tracked in `max_fc`. Grade = `score / max
possible (all-perfect sim)` → **A ≥90% / B 70–89% / C 50–69% / D <50%**. Accuracy on results:
`(perfect*1.0 + good*0.85 + meh*0.6)/total*100`.
Settings (persisted `config.json`): `input_latency` (feeds `beat_offset`), `music_volume`
(player + preview at ×0.5), `fx_volume`, `video_brightness` (video dim), `lane_alpha` (lane/beat
opacity), plus editable `keybinds` rebuilding global `KEY_TO_LANE`.

### 1.3 Beatmap Philosophy

- **Easy** `~1.45 beats/sec` — voice/melody only, fallback to drums only in gaps `>3.5s` where
  no voice exists (`detect_beats_madmom`).
- **Medium** `~2.1 beats/sec` — melody + snare/bass.
- **Hard** `~3.0 beats/sec` — voice-priority but retains percussive fills.
- Lane assignment is pitch-aware: spectral centroid `150-4000 Hz` (`_centroids_for_times`)
  quantile `0..1` → `pref D/F/J/K`, with an ergonomic score that penalises `recency` and
  same-lane repeats (`beatmap_from_times`).
- Tempo via autocorrelation (`estimate_tempo_autocorr 55-200 BPM`), cached per difficulty to
  `songs/.cache/<stem>_<md5[:8]>_{easy|medium|hard}.json` (cache v4, stores rating).

---

## 2. Wireframes — Main Screens

> Palette: bg `10,10,18`, panels `22,22,34` / `18,18,30`, selected `55,55,90`, accent
> `100,255,160`, border `120,180,255`, feedback `PERFECT yellow 255,240,80 / GOOD green`
> `100,255,150 / OK blue 100,200,255 / MISS red 255,80,80`

### 2.1 Menu — osu!-style big play button

```
RADIAL RHYTHM  (40pt bold white)                    H-120
D F J K  •  songs in ./songs/ (N found)              H-155

              +----------------------------+
              |  PLAY  (big pill, glows)   |   y centre
              +----------------------------+
              |  OPEN FILE                 |
              |  SETTINGS                  |
              |  QUIT                      |
```
- `menu_options = ["PLAY","OPEN FILE","SETTINGS","QUIT"]`, `UP/DOWN|W/S` cycle, `ENTER/SPACE`
  selects, `ESC` quit, `O` open dialog, `F11` fullscreen, `F1` FPS.

### 2.2 Song Select — osu!-style carousel + live preview

```
full-bleed live video preview backdrop (dimmed overlay)
LEFT column (title + meta)          RIGHT vertical carousel
  song title  (x=90, H-108)            card index 0 at top (UP = visual up)
  song meta (H-142)
  3 difficulty buttons (320x56)        cards 330x92, 112 px apart
    EASY    d1  1.45/s                 selected card pulls out: scale 1.16x,
    MEDIUM  d9  2.10/s                 nudged left, accent edge + top/bottom rings
    HARD    d20 3.00/s
```
- `UP/DOWN|W/S` browse + click; `LEFT/RIGHT` or `1/2/3` pick difficulty; `ENTER` plays;
  `R` refresh; `B/ESC` back.

### 2.3 Difficulty Select

```
CHOOSE DIFFICULTY                     panel 640x340 (18,18,30)
  EASY    melody & voice • ~1.5/s • playable
  MEDIUM  melody + drums • ~2.1/s • moderate
  HARD    full groove   • ~3.0/s • dense challenge
UP/DOWN/LEFT/RIGHT choose • ENTER confirm • 1/2/3 quick • ESC back
```

### 2.4 Analyzing

```
ANALYSING  (22pt bold, 255,220,100)
  Miku by Anamanaguchi ...mp4   [EASY] 42%
  [####...####]  520x18  shimmer anim
First load analyses via ffmpeg • next load instant (cached)
ESC to cancel
```
- Threaded (`_analysis_thread_func`) with progress `0..1`; `ESC/B` cancels to `song_select`.

### 2.5 Playing

```
video texture cover (dimmed)                    top bar: score, combo, time, mode [EASY]
spiral lane guides (curved polylines, lane colour)
outer spawn rings at each predecessor side       progress bar at y=6
●●● beats spiraling inward (0.9+0.35*raw)
◎  centre target ring
D F J K hit • SPACE pause • ESC menu
```
- `spawn_beats` when `bt_eff - song_t <= TRAVEL_TIME+0.05`; `get_song_time` prefers
  `media_player.time`.
- Pooled `game_batch` (36 beats max) — `circle 22 / inner 12 / hit 28` + per-slot ghost arc
  segments for the new-section telegraph.

### 2.6 Paused

```
[ dim 120 ]
  PAUSED  36pt bold white
  SPACE to resume  |  ESC for menu
```

### 2.7 Results

```
[ dim 130 ]
  card 560x360
  RESULTS
  Score, Max Combo, PERFECT/GOOD/OK/MISS, Accuracy %
  ENTER / SPACE / ESC : back to menu    R : replay
```

---

## 3. User Flow Diagram

> Shape key (Blackjack-style): `Oval` = Start/End, `Rectangle` = Screen, `Diamond` = Decision,
> `Parallelogram` = Action.

```mermaid
flowchart TD
    A([START<br/>launch main.py<br/>ffmpeg check]) --> B[Rectangle: MENU]
    B -->|UP/DOWN or W/S<br/>ENTER/SPACE| C{Diamond: Menu option?}
    C -->|PLAY| D[Rectangle: SONG_SELECT<br/>carousel + preview]
    C -->|OPEN FILE| O1[Parallelogram: dialog<br/>load_media] --> B
    C -->|SETTINGS| S1[Rectangle: SETTINGS<br/>fullscreen]
    C -->|QUIT / ESC| Z([END])
    D -->|UP/DOWN, R refresh<br/>mouse pick| G{Diamond: Song chosen?}
    G -->|No| D
    G -->|Yes| H[Rectangle: DIFFICULTY_SELECT<br/>EASY / MEDIUM / HARD]
    H -->|1/2/3, ←/→| J{Diamond: Confirm?}
    J -->|ENTER| K[Parallelogram: load_media<br/>difficulty, autoplay=True]
    J -->|B/ESC| D
    K --> L{Diamond: Cache hit?<br/>stem_hash_{diff}.json v4}
    L -->|Yes| M[Rectangle: Ready cached<br/>N beats + rating] -->|ENTER| N[Rectangle: PLAYING<br/>spiral beats]
    L -->|No| A1[Rectangle: ANALYZING<br/>progress bar]
    A1 -->|ESC/B| D
    A1 -->|worker: ffmpeg→wav→madmom<br/>harmonic weighting| P{Diamond: Done?}
    P -->|Error| D
    P -->|Success, save cache| M
    N --> Q{Diamond: Song end?}
    Q -->|No| N
    Q -->|Yes| R[Rectangle: RESULTS]
    N -->|SPACE| PA[Rectangle: PAUSED]
    PA -->|SPACE| N
    PA -->|ESC| B
    N -->|ESC| B
    N -->|D/F/J/K| T{Diamond: Hit window?}
    T -->|PERFECT/GOOD/OK| N
    T -->|MISS| N
    R -->|ENTER/SPACE/ESC| B
    R -->|R| N
```

---

## 4. Main Mechanic — Pseudocode & Diagrams

### 4.1 Spiral Convergence (current)

```python
# spawn
prev_t = active_beats[-1].time if present else None
new_section = prev_t is None or (bt_eff - prev_t) > 2.0
cw = not new_section                       # new section → counterclockwise
start_ang = (hit_ang + 90) % 360 if cw else (hit_ang - 90) % 360

# each frame
song_t = media_player.time if is_media_mode else time.time() - start_time
raw = (beat.time - song_t) / TRAVEL_TIME   # 1 at spawn, 0 at centre
radius = SPAWN_RADIUS - raw*(SPAWN_RADIUS - TARGET_RADIUS)   # 520 → 70
ang    = start_ang - raw*90 if cw else start_ang + raw*90    # 90° sweep
pos    = (CENTER.x + cos(ang)*radius, CENTER.y + sin(ang)*radius)
# new-section (CCW) notes draw a faint ghost spiral arc telegraph
# hit burst 0.25s: radius = TARGET + prog*30
# miss fade 0.45s: grey dim, alpha fades
```

### 4.2 Hit Detection & Scoring

```python
def try_hit(input_lane):
    best = min(active_beats where lane==input_lane and not hit and not missed
               by abs(song_t - beat.time))
    if best is None: combo -= ; feedback MISS; return
    delta = abs(song_t - best.time)
    if   delta <= 0.13: 300 perfect
    elif delta <= 0.26: 150 good
    elif delta <= 0.35:  50 ok
    else: MISS
    best.hit=True; combo+=1
    score += int(pts * (1 + min(combo//8,4)*0.25))
```

### 4.3 Beatmap Generation — Voice-First

```python
def beats_from_media(path, difficulty, sensitivity):
    ffmpeg → mono 44100 wav
    acts = RNNOnsetProcessor(audio, fps=100)
    voice_w = harmonic_voice_weights(sr, audio, len(acts))   # STFT flux harm/(harm+perc)
    acts_w  = acts * (voice_floor + (1-voice_floor)*voice_w)
    thr,comb,mg,target = easy(0.32,0.12,0.38,1.45) / med(0.30,0.11,0.26,2.1) / hard(0.28,0.10,0.18,3.0)
    beats = peak_pick(acts_w, thr, comb) + min_gap mg
    keep strongest up to target; fill silent gaps
    bpm = autocorrelation tempo
    beatmap = beatmap_from_times(times, dur, sr, audio)  # pitch centroid → lanes
    cache v4 {beatmap, duration, tempo, rating}
```

### 4.4 Lane Assignment Diagram

```
centroid 6033 (low)  → rank 0.1 → pref D → candidate order D,F,J,K → score + recency → pick D
centroid 7574 (high) → rank 0.9 → pref K → pick K (if not recent)
→ avg per lane verified: D 6033 < F 6705 < J 7330 < K 7574 (Miku easy)
```

---

## 5. Assumptions & Open Questions

- **Voice model:** I chose a lightweight harmonic/percussive flux ratio (`~9s/30s`) over heavy
  `spleeter/demucs` or `librosa.effects.hpss` (`22s/10s`). *Question: keep lightweight or accept a
  heavier vocal separator for higher voice accuracy?*
- **Densities:** `1.45/2.1/3.0` for easy/medium/hard. Should these be user-slidable or fixed per
  difficulty? The easy gap fallback `>3.5s` — tighten to `>2.5s` for fewer silences?
- **Spiral direction:** default clockwise, counterclockwise only on new sections. Should I ever
  alternate per beat (bullet-hell style), or keep it section-driven?
- **Wireframes:** above is low-fi code-driven (existing `pyglet.shapes` palette). Should I do a
  high-fi Figma export or keep as is?
- **Flow on replay:** `R` in `RESULTS` replays the same difficulty; should it cycle difficulty or
  return to `difficulty_select`?

---

## 6. Next Steps

1. Validate easy voice recall on a vocal-sparse track (`test_video.mp4`) — confirm the gap
   fallback isn't too sparse.
2. User-test `difficulty_select → analyzing → playing` for cached vs uncached paths.
3. If high-fi wanted, produce Figma wireframes from the existing shapes.
4. Maybe add a subtle ghost arc on clockwise notes too, and/or a "NEW SECTION" flash.
